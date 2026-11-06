"""PoC: Quartz 5 (static site generator) as an optional, per-vault publish renderer.

Nothing here replaces the existing renderer. A vault opts in by having
`publish_meta/{owner}/{vault}/renderer.json == {"renderer": "quartz"}`; every other vault keeps
going through the unchanged `_render_publish_page` path. See docs/QUARTZ_POC.ko.md for what was
verified and what was not.

Responsibilities, all deliberately outside Quartz itself (Quartz only turns a content folder into
static files):

  * build_site()        -- stage published files + per-vault config, run Quartz, swap output in
                           atomically (a failed build never touches the live site)
  * BuildScheduler      -- debounce/serialise rebuilds per vault
  * resolve()           -- map an incoming URL onto the built output using the *existing* routing
                           precedence (permalink -> literal path -> alias -> 404)
  * adapt_publish_css() / inject_head() -- publish.css / publish.js compatibility
  * password gate       -- cookie-based, enforced server-side (pumice has no gate today)
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote

import yaml

logger = logging.getLogger(__name__)

RENDERER_META_FILENAME = "renderer.json"
QUARTZ_DIR_ENV = "PUMICE_QUARTZ_DIR"

# Plugins switched off for a multi-tenant server: og-image costs ~7s/100 notes (1000 notes: 3min
# -> 24s without it), encrypted-pages is per-page client-side crypto, a different model from the
# site-level password this server has.
_DISABLED_PLUGIN_SUFFIXES = ("/og-image", "/encrypted-pages", "/quartz-fonts")  # quartz-fonts links Google Fonts

# publish.css theme variables (docs/PUBLISH_CSS.md) -> Quartz's theme variables. Only variables
# that have a sensible Quartz counterpart are mapped; structural `.pumice-*` selectors have no
# equivalent in Quartz's DOM and are intentionally NOT emulated.
PUBLISH_CSS_VAR_MAP = {
    "--bg-color": "--light",
    "--border-color": "--lightgray",
    "--text-muted": "--gray",
    "--text-primary": "--darkgray",
    "--heading-color": "--dark",
    "--accent-blue": "--secondary",
    "--wiki-link": "--tertiary",
}


# ─── opt-in flag ────────────────────────────────────────────────────────────────────────────────

def _meta_path(data_dir: str, owner: str, vault_id: str) -> str:
    return os.path.join(data_dir, "publish_meta", owner, vault_id, RENDERER_META_FILENAME)


def vault_uses_quartz(data_dir: str, owner: str, vault_id: str) -> bool:
    try:
        with open(_meta_path(data_dir, owner, vault_id), "r", encoding="utf-8") as f:
            return json.load(f).get("renderer") == "quartz"
    except (OSError, ValueError):
        return False


def set_vault_renderer(data_dir: str, owner: str, vault_id: str, renderer: Optional[str]) -> None:
    path = _meta_path(data_dir, owner, vault_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"renderer": renderer}, f)


def site_out_dir(data_dir: str, owner: str, vault_id: str) -> str:
    return os.path.join(data_dir, "quartz_out", owner, vault_id)


# ─── build status (survives restarts; also the "dirty" marker for restart recovery) ─────────────

STATUS_FILENAME = "quartz_build.json"
_STATUS_STATES = ("pending", "building", "ok", "failed")


def _status_path(data_dir: str, owner: str, vault_id: str) -> str:
    return os.path.join(data_dir, "publish_meta", owner, vault_id, STATUS_FILENAME)


def read_status(data_dir: str, owner: str, vault_id: str) -> dict:
    try:
        with open(_status_path(data_dir, owner, vault_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"state": None}


def write_status(data_dir: str, owner: str, vault_id: str, state: str, **extra) -> dict:
    """state: pending (a change is waiting to be built), building, ok, failed. A pending/building
    state found at startup means the process died mid-way: recover_candidates() rebuilds it."""
    assert state in _STATUS_STATES
    status = {"state": state, "updated_at": int(time.time() * 1000), **extra}
    path = _status_path(data_dir, owner, vault_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False)
    os.replace(tmp, path)
    return status


def recover_candidates(data_dir: str) -> list:
    """(owner, vault_id) of every Quartz-rendered vault that needs a build after a restart: no built
    site yet, or the last build was interrupted (pending/building)."""
    found = []
    base = os.path.join(data_dir, "publish_meta")
    if not os.path.isdir(base):
        return found
    for owner in sorted(os.listdir(base)):
        for vault_id in sorted(os.listdir(os.path.join(base, owner))) if os.path.isdir(os.path.join(base, owner)) else []:
            if not vault_uses_quartz(data_dir, owner, vault_id):
                continue
            built = os.path.isfile(os.path.join(site_out_dir(data_dir, owner, vault_id), "static", "contentIndex.json"))
            if not built or read_status(data_dir, owner, vault_id).get("state") in ("pending", "building"):
                found.append((owner, vault_id))
    return found


# ─── build ──────────────────────────────────────────────────────────────────────────────────────

# publish_options keys that can only be applied while building (everything else is applied per
# request, so changing it never needs a rebuild). See docs/QUARTZ_OPTIONS.ko.md.
BUILD_TIME_OPTION_KEYS = ("siteName", "strictLineBreaks", "slidingWindowMode")


def render_quartz_config(
    default_yaml_text: str, *, base_url: str, page_title: str, locale: str = "ko-KR",
    strict_line_breaks: bool = False, sliding_window: bool = False,
) -> str:
    cfg = yaml.safe_load(default_yaml_text)
    conf = cfg["configuration"]
    conf["baseUrl"] = base_url
    conf["pageTitle"] = page_title
    conf["locale"] = locale
    # Privacy: Quartz ships with plausible.io analytics ON and Google Fonts/CDN links ON, which would
    # make every published site report its visitors to third parties. Analytics is instead
    # driven by the `googleAnalytics` site option (request time, custom domain only).
    conf["analytics"] = None
    theme = conf.setdefault("theme", {})
    theme["cdnCaching"] = False
    theme["fontOrigin"] = "local"
    toggles = {"/hard-line-breaks": strict_line_breaks, "/stacked-pages": sliding_window}
    for plugin in cfg.get("plugins", []):
        src = plugin["source"]
        if src.endswith(_DISABLED_PLUGIN_SUFFIXES):
            plugin["enabled"] = False
        for suffix, enabled in toggles.items():
            if src.endswith(suffix):
                plugin["enabled"] = enabled
        if src.endswith("/footer"):
            plugin.setdefault("options", {})["links"] = {}  # drop the Quartz GitHub/Discord promo links
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


def split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter_block_including_fences_and_newline, body). Empty frontmatter if none."""
    m = re.match(r"\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if not m:
        return "", text
    return text[: m.end()], text[m.end():]


def adapt_publish_css(css: str) -> str:
    """Duplicates each mapped pumice variable declaration as its Quartz counterpart, and maps the
    `[data-theme]` selectors onto Quartz's `[saved-theme]` attribute."""
    out = css.replace('[data-theme="dark"]', '[saved-theme="dark"]').replace('[data-theme="light"]', '[saved-theme="light"]')
    pattern = r"(" + "|".join(re.escape(k) for k in PUBLISH_CSS_VAR_MAP) + r")\s*:\s*([^;}]+)"
    return re.sub(pattern, lambda m: f"{m.group(1)}:{m.group(2)};{PUBLISH_CSS_VAR_MAP[m.group(1)]}:{m.group(2)}", out)


def synthetic_index_markdown(title: str, md_paths: list) -> str:
    """Root page for a vault with no index.md. The existing renderer lists every published file at
    the root in that case; Quartz would 404. `unlisted` keeps this page out of the explorer, graph,
    search and RSS so it is only reachable as the root URL."""
    items = "\n".join(f"- [[{p[:-3]}]]" for p in sorted(md_paths))
    safe_title = title.replace('"', '\\"')
    return f'---\ntitle: "{safe_title}"\nunlisted: true\n---\n{items}\n'


_PRECONNECT_RE = re.compile(r'<link\s+rel="preconnect"\s+href="https://[^"]*"[^>]*/?>')


class QuartzBuildError(RuntimeError):
    pass


def build_site(
    published_base: str, out_dir: str, *, template_dir: str, base_url: str, page_title: str,
    transform_md: Optional[Callable[[str, str], str]] = None, timeout: int = 600, locale: str = "ko-KR",
    site_options: Optional[dict] = None,
) -> dict:
    """Builds `published_base` with Quartz into `out_dir`. The previous `out_dir` stays live
    until the new build fully succeeds, then is swapped in with two renames.

    template_dir: a Quartz checkout with node_modules installed (shared, read-only). Every build
    gets its own throwaway workspace (copy of the checkout minus node_modules/content, node_modules
    symlinked) because Quartz reads quartz.config.yaml/custom.scss/content from its own cwd.
    transform_md(rel_path, body) -> body: the server-plugin hook, applied to each note's body with
    frontmatter kept out of the plugin's sight.
    """
    node = shutil.which("node")
    if not node:
        raise QuartzBuildError("node executable not found")
    parent = os.path.dirname(os.path.abspath(out_dir))
    os.makedirs(parent, exist_ok=True)
    workspace = tempfile.mkdtemp(prefix=".quartz-build-", dir=parent)
    started = time.monotonic()
    try:
        q = os.path.join(workspace, "q")
        shutil.copytree(
            template_dir, q, symlinks=True,
            ignore=shutil.ignore_patterns("node_modules", ".git", "content", "docs", "out", "quartz.config.yaml"),
        )
        os.symlink(os.path.join(template_dir, "node_modules"), os.path.join(q, "node_modules"))

        content = os.path.join(q, "content")
        md_paths = []
        for root, _dirs, files in os.walk(published_base):
            for name in files:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, published_base).replace(os.sep, "/")
                dst = os.path.join(content, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if rel.lower().endswith(".md"):
                    md_paths.append(rel)
                if transform_md and rel.lower().endswith(".md"):
                    with open(src, "r", encoding="utf-8") as f:
                        fm, body = split_frontmatter(f.read())
                    with open(dst, "w", encoding="utf-8") as f:
                        f.write(fm + transform_md(rel, body))
                else:
                    shutil.copy2(src, dst)

        if not os.path.exists(os.path.join(content, "index.md")):
            with open(os.path.join(content, "index.md"), "w", encoding="utf-8") as f:
                f.write(synthetic_index_markdown((site_options or {}).get("siteName") or page_title, md_paths))

        with open(os.path.join(template_dir, "quartz.config.default.yaml"), "r", encoding="utf-8") as f:
            default_yaml = f.read()
        with open(os.path.join(q, "quartz.config.yaml"), "w", encoding="utf-8") as f:
            opts = site_options or {}
            f.write(render_quartz_config(
                default_yaml, base_url=base_url, page_title=opts.get("siteName") or page_title, locale=locale,
                strict_line_breaks=bool(opts.get("strictLineBreaks")), sliding_window=bool(opts.get("slidingWindowMode")),
            ))

        proc = subprocess.run(
            [node, "quartz/bootstrap-cli.mjs", "build", "-d", "content", "-o", "out"],
            cwd=q, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise QuartzBuildError(f"quartz build failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-2000:]}")

        built = os.path.join(q, "out")
        if not os.path.isfile(os.path.join(built, "static", "contentIndex.json")):
            raise QuartzBuildError("quartz build produced no contentIndex.json")

        css_src = os.path.join(published_base, "publish.css")
        if os.path.isfile(css_src):
            with open(css_src, "r", encoding="utf-8") as f:
                adapted = adapt_publish_css(f.read())
            with open(os.path.join(built, "publish.css"), "w", encoding="utf-8") as f:
                f.write(adapted)

        # Quartz still emits a preconnect to cdnjs (it opens a connection, i.e. exposes the visitor's
        # IP, although nothing is ever fetched from there). Strip it from every built page.
        for page_root, _dirs, names in os.walk(built):
            for n in names:
                if n.endswith(".html"):
                    page = os.path.join(page_root, n)
                    with open(page, "r", encoding="utf-8") as f:
                        text = f.read()
                    cleaned = _PRECONNECT_RE.sub("", text)
                    if cleaned != text:
                        with open(page, "w", encoding="utf-8") as f:
                            f.write(cleaned)

        old = None
        if os.path.exists(out_dir):
            old = f"{out_dir}.old-{int(time.time() * 1000)}"
            os.rename(out_dir, old)
        os.rename(built, out_dir)
        if old:
            shutil.rmtree(old, ignore_errors=True)
        return {"seconds": round(time.monotonic() - started, 2)}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ─── scheduler ──────────────────────────────────────────────────────────────────────────────────

class BuildScheduler:
    """Debounces notify() per key and guarantees at most one build per key at a time. A change that
    arrives mid-build marks the key dirty and triggers exactly one more build afterwards."""

    def __init__(self, build_fn: Callable[[tuple, object], None], debounce_seconds: float = 3.0, max_concurrent: int = 2):
        self._build_fn = build_fn
        self._debounce = debounce_seconds
        # A Quartz build is CPU-heavy (a 1,000-note vault takes ~25s of CPU); cap how many different
        # vaults build at the same time so a burst of publishes can't starve the web server.
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        self._lock = threading.Lock()
        self._timers: dict = {}
        self._payloads: dict = {}
        self._running: set = set()
        self._dirty: set = set()
        self._idle = threading.Condition(self._lock)

    def notify(self, key: tuple, payload: object = None, *, immediate: bool = False) -> None:
        with self._lock:
            self._payloads[key] = payload
            timer = self._timers.pop(key, None)
            if timer:
                timer.cancel()
            timer = threading.Timer(0 if immediate else self._debounce, self._fire, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, key: tuple) -> None:
        with self._lock:
            self._timers.pop(key, None)
            if key in self._running:
                self._dirty.add(key)
                return
            self._running.add(key)
            payload = self._payloads.get(key)
        try:
            with self._slots:
                self._build_fn(key, payload)
        except Exception:
            logger.exception("Quartz build failed for %s (previous output left in place)", key)
        finally:
            with self._lock:
                self._running.discard(key)
                rerun = key in self._dirty
                self._dirty.discard(key)
                self._idle.notify_all()
            if rerun:
                self._fire(key)

    def wait_idle(self, timeout: float = 600.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._timers or self._running or self._dirty:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(min(remaining, 0.1))
        return True


# ─── URL resolution ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SiteIndex:
    file_to_slug: dict   # vault file path ("폴더A/노트2.md") -> Quartz slug ("폴더a/노트2")
    slugs: set
    slugs_lower: dict    # lowercase slug -> slug
    slug_to_file: dict = None   # reverse of file_to_slug
    files_lower: dict = None    # lowercase built-file path -> real built path (Quartz lowercases asset folders: Img/Logo.PNG -> img/logo.PNG)


_INDEX_CACHE: dict = {}  # (out_dir, mtime_ns) -> SiteIndex; a rebuild swaps in a new file -> new key


def load_site_index(out_dir: str) -> SiteIndex:
    index_path = os.path.join(out_dir, "static", "contentIndex.json")
    key = (out_dir, os.stat(index_path).st_mtime_ns)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    with open(index_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    entries = raw.get("entries", raw) if isinstance(raw, dict) else {}
    file_to_slug, slugs = {}, set()
    for slug, e in entries.items():
        slugs.add(slug)
        if e.get("filePath"):
            file_to_slug[e["filePath"]] = slug
    files_lower = {}
    for root, _dirs, names in os.walk(out_dir):
        for n in names:
            rel = os.path.relpath(os.path.join(root, n), out_dir).replace(os.sep, "/")
            files_lower.setdefault(rel.lower(), rel)
    result = SiteIndex(
        file_to_slug, slugs, {s.lower(): s for s in slugs},
        slug_to_file={v: k for k, v in file_to_slug.items()}, files_lower=files_lower,
    )
    for stale in [k for k in _INDEX_CACHE if k[0] == out_dir]:
        del _INDEX_CACHE[stale]
    _INDEX_CACHE[key] = result
    return result


def _safe_join(base: str, rel: str) -> Optional[str]:
    base = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base, rel))
    return full if full == base or full.startswith(base + os.sep) else None


def resolve(
    out_dir: str, index: SiteIndex, path: str, *,
    by_permalink: Callable[[str], Optional[str]], by_alias: Callable[[str], Optional[str]],
    index_file: Optional[str] = None,
) -> tuple:
    """Same precedence as the existing renderer (permalink -> literal path -> alias -> 404), except
    that every non-canonical hit is a *redirect* to the Quartz slug instead of being served in
    place: Quartz pages use depth-relative links (`../노트1`), so a page served under a different
    URL depth than its own slug would resolve every link wrongly.

    Returns ("file", abs_path) | ("redirect", slug) | ("notfound", None) | ("forbidden", None)."""
    path = path.strip("/")

    target = by_permalink(path) if path else None
    if target and target in index.file_to_slug:
        return "redirect", index.file_to_slug[target]

    if path == "":
        # `indexFile` site option: the root shows that note (only when it is actually published).
        # Redirect rather than serve in place for the same relative-link reason as permalinks.
        if index_file and index_file in index.file_to_slug:
            return "redirect", index.file_to_slug[index_file]
        return "file", os.path.join(out_dir, "index.html")

    direct = _safe_join(out_dir, path)
    if direct is None:
        return "forbidden", None
    if os.path.isfile(direct):
        return "file", direct

    if path in index.slugs:
        for cand in (f"{path}.html", os.path.join(path, "index.html")):
            p = _safe_join(out_dir, cand)
            if p and os.path.isfile(p):
                return "file", p
    # Folder listing pages (folder-page plugin) have no contentIndex entry.
    folder_index = _safe_join(out_dir, os.path.join(path, "index.html"))
    if folder_index and os.path.isfile(folder_index):
        return "file", folder_index

    for literal in (path, path + ".md"):
        if literal in index.file_to_slug:
            slug = index.file_to_slug[literal]
            return "redirect", slug

    lowered = path.lower()
    if lowered in index.slugs_lower and index.slugs_lower[lowered] != path:
        return "redirect", index.slugs_lower[lowered]

    # Asset folders are lowercased by Quartz (Img/Logo.PNG is built as img/logo.PNG); an existing
    # URL, a logo option or a favicon link still uses the original spelling.
    real = (index.files_lower or {}).get(path.lower())
    if real:
        p = _safe_join(out_dir, real)
        if p and os.path.isfile(p):
            return "file", p

    alias_target = by_alias(path)
    if alias_target and alias_target in index.file_to_slug:
        return "redirect", index.file_to_slug[alias_target]

    return "notfound", None


# ─── publish.css / publish.js injection ─────────────────────────────────────────────────────────

# Quartz has no #pumice-note-root; the documented publish.js recipe waits for it. Alias it onto
# Quartz's <article> (re-applied after each SPA navigation) so that recipe keeps working.
_COMPAT_SHIM_JS = (
    "(function(){function a(){var e=document.querySelector('article');"
    "if(e&&!document.getElementById('pumice-note-root'))e.id='pumice-note-root';}"
    "a();document.addEventListener('DOMContentLoaded',a);document.addEventListener('nav',a);})();"
)


def inject_head(
    html_text: str, *, css_href: Optional[str], js_href: Optional[str], preload: Optional[dict], extra_head: str = "",
) -> str:
    parts = [extra_head] if extra_head else []  # site options first, so publish.css (below) still wins
    if css_href:
        parts.append(f'<link rel="stylesheet" href="{html.escape(css_href, quote=True)}">')
    if js_href:
        payload = json.dumps(preload or {}, ensure_ascii=False).replace("</", "<\\/")
        parts.append(f"<script>window.pumicePreload={payload};{_COMPAT_SHIM_JS}</script>")
        parts.append(f'<script src="{html.escape(js_href, quote=True)}" defer></script>')
    if not parts:
        return html_text
    return html_text.replace("</head>", "".join(parts) + "</head>", 1)


def root_asset_href(url_prefix: str, filename: str) -> str:
    return f"{url_prefix}{quote(filename, safe='')}"


# ─── per-request site options (publish_options) ─────────────────────────────────────────────────
# Everything below is applied while serving, so changing these options never needs a rebuild. The
# defaults come from web._SITE_OPTIONS_DEFAULTS, NOT from Quartz: e.g. the existing site hides the
# navigation tree and the theme toggle and renders dark unless the owner opts in, while Quartz shows
# the explorer and toggle and follows the OS theme.

_SAFE_CSS_URL = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&*+,;=%-]+$")
_GA_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def resolve_asset_href(index: "SiteIndex", value: Optional[str], url_prefix: str) -> Optional[str]:
    """URL of a vault file (logo, favicon, og image) or an external http(s) URL as-is. A vault path
    is looked up case-insensitively against the built output because Quartz lowercases folders."""
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    real = (index.files_lower or {}).get(value.lower())
    return f"{url_prefix}{quote(real, safe='/')}" if real else None


def options_css(opts: dict, logo_href: Optional[str]) -> str:
    rules = []
    if not opts.get("showNavigation"):
        rules.append(".explorer{display:none}")
    if not opts.get("showSearch"):
        rules.append(".search{display:none}")
    if not opts.get("showThemeToggle"):
        rules.append(".darkmode{display:none}")
    if opts.get("hideTitle"):
        rules.append(".article-title{display:none}")
    if not opts.get("readableLineLength"):
        rules.append(".page{max-width:none}")
    if logo_href and _SAFE_CSS_URL.match(logo_href):
        rules.append(
            ".page-title a::before{content:'';display:inline-block;width:1.2em;height:1.2em;margin-right:.4em;"
            f"vertical-align:-.15em;background:url('{logo_href}') center/contain no-repeat}}"
        )
    return f"<style>{''.join(rules)}</style>" if rules else ""


def theme_script(default_theme: str) -> str:
    """'light'/'dark' are forced until the visitor picks one (Quartz stores that in localStorage
    'theme'); anything else ('system') keeps Quartz's own OS-preference behaviour. Runs after
    Quartz's prescript, which has already applied the OS preference."""
    if default_theme not in ("light", "dark"):
        return ""
    return (
        "<script>(function(){try{if(!localStorage.getItem('theme'))"
        f"document.documentElement.setAttribute('saved-theme','{default_theme}')}}catch(e){{}}}})();</script>"
    )


def ga_snippet(ga_id: str) -> str:
    """Same gtag.js snippet as the existing template. Caller gates it on a custom domain."""
    if not ga_id or not _GA_ID.match(ga_id):
        return ""
    return (
        f'<script async src="https://www.googletagmanager.com/gtag/js?id={ga_id}"></script>'
        "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
        f"gtag('js',new Date());gtag('config','{ga_id}');</script>"
    )


def navigation_order(published_paths: list, ordering: list, hidden: list) -> tuple:
    """(visible top-level names in display order, hidden top-level names). Same algorithm as
    web._build_navigation_tree (which only orders/hides the TOP level, like real Obsidian)."""
    top, seen = {}, []
    for p in published_paths:
        name = p.split("/", 1)[0]
        if name not in top:
            top[name] = "/" in p
            seen.append(name)
    hidden_set = set(hidden)
    ordered = [n for n in ordering if n in top and n not in hidden_set]
    remaining = sorted(n for n in seen if n not in hidden_set and n not in ordered)
    return ordered + remaining, [n for n in seen if n in hidden_set]


def navigation_keys(index: "SiteIndex", published_paths: list, ordering: list, hidden: list) -> tuple:
    """navigation_order() translated into the keys the explorer script matches on: 'f:<slug>' for a
    top-level note, 'd:<first slug segment>' for a folder. Only pages Quartz actually lists."""
    md = [p for p in published_paths if p in index.file_to_slug]
    order, hid = navigation_order(md, ordering, hidden)

    def key(name: str) -> Optional[str]:
        if name in index.file_to_slug:
            return "f:" + index.file_to_slug[name]
        for p in md:
            if p.startswith(name + "/"):
                return "d:" + index.file_to_slug[p].split("/")[0]
        return None

    return [k for k in map(key, order) if k], [k for k in map(key, hid) if k]


# Quartz builds the explorer in the browser (and re-renders it on every SPA navigation), so
# ordering/hiding must be applied there; Quartz's own sortFn/filterFn cannot be set from YAML.
_NAV_JS = (
    "(function(c){function key(li){var f=li.querySelector(':scope > .folder-container');"
    "if(f)return 'd:'+(f.getAttribute('data-folderpath')||'').split('/')[0];"
    "var a=li.querySelector(':scope > a.nav-file-title');if(a){var p=decodeURIComponent(new URL(a.href,location.href).pathname);"
    "var b=document.body.dataset.basepath||'';if(b&&p.indexOf(b)===0)p=p.slice(b.length);return 'f:'+p.replace(/^\\/+/,'');}return null;}"
    "function apply(){document.querySelectorAll('.explorer-ul').forEach(function(ul){"
    "var items=[].slice.call(ul.children).filter(function(li){return !li.classList.contains('overflow-end');});"
    "var map={},keys=[];items.forEach(function(li){var k=key(li);keys.push(k);if(k)map[k]=li;});"
    "c.hidden.forEach(function(k){if(map[k]&&map[k].style.display!=='none')map[k].style.display='none';});"
    "var first=c.order.filter(function(k){return map[k];});"
    "var rest=items.filter(function(li){return first.indexOf(key(li))===-1;});"
    "var want=first.concat(rest.map(key));if(want.join('\\n')===keys.join('\\n'))return;"
    "first.map(function(k){return map[k];}).concat(rest).forEach(function(li){ul.appendChild(li);});});}"
    "var queued=false;function run(){if(queued)return;queued=true;requestAnimationFrame(function(){queued=false;apply();});}"
    "document.addEventListener('DOMContentLoaded',function(){run();new MutationObserver(run).observe(document.body,{childList:true,subtree:true});});"
    "document.addEventListener('nav',run);})(__CFG__);"
)


def nav_script(order_keys: list, hidden_keys: list) -> str:
    cfg = json.dumps({"order": order_keys, "hidden": hidden_keys}, ensure_ascii=False).replace("</", "<\\/")
    return "<script>" + _NAV_JS.replace("__CFG__", cfg) + "</script>"


def set_meta(html_text: str, key: str, value: str) -> str:
    """Replaces (or adds) one <meta property|name=key> tag. Quartz emits og:/twitter: tags from the
    note body; a note's own `description`/`image` front matter must win, as in the existing renderer."""
    tag = f'<meta property="{key}" content="{html.escape(value, quote=True)}">'
    pattern = re.compile(r'<meta\b[^>]*\b(?:property|name)="' + re.escape(key) + r'"[^>]*>')
    if pattern.search(html_text):
        return pattern.sub(lambda _m: tag, html_text, count=1)
    return html_text.replace("</head>", tag + "</head>", 1)


def favicon_links_html(links: list) -> str:
    out = []
    for link in links:
        sizes = f' sizes="{html.escape(link["sizes"], quote=True)}"' if link.get("sizes") else ""
        out.append(f'<link rel="icon" href="{html.escape(link["href"], quote=True)}"{sizes}>')
    return "".join(out)


# ─── password gate ──────────────────────────────────────────────────────────────────────────────

COOKIE_TTL_SECONDS = 7 * 24 * 3600


def _fingerprint(passwords: list) -> str:
    return hashlib.sha256("\n".join(sorted(p["pw"] for p in passwords)).encode()).hexdigest()


def make_cookie(secret: str, owner: str, vault_id: str, passwords: list, now: Optional[float] = None) -> str:
    ts = int(now if now is not None else time.time())
    msg = f"{owner}/{vault_id}/{ts}/{_fingerprint(passwords)}"
    return f"{ts}.{hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()}"


def cookie_valid(secret: str, owner: str, vault_id: str, passwords: list, cookie: Optional[str], now: Optional[float] = None) -> bool:
    if not cookie or "." not in cookie:
        return False
    ts_text, _, _sig = cookie.partition(".")
    if not ts_text.isdigit():
        return False
    current = now if now is not None else time.time()
    if current - int(ts_text) > COOKIE_TTL_SECONDS:
        return False
    return hmac.compare_digest(cookie, make_cookie(secret, owner, vault_id, passwords, now=int(ts_text)))


def password_matches(passwords: list, candidate: str) -> bool:
    return any(hmac.compare_digest(p["pw"].encode(), candidate.encode()) for p in passwords)


def cookie_name(owner: str, vault_id: str) -> str:
    return "pumice_pw_" + hashlib.sha256(f"{owner}/{vault_id}".encode()).hexdigest()[:16]


def login_page_html(*, error: bool) -> str:
    msg = '<p style="color:#b42318">비밀번호가 올바르지 않습니다.</p>' if error else ""
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>비밀번호 필요</title></head><body style=\"font-family:sans-serif;max-width:360px;margin:20vh auto;padding:0 16px\">"
        f"<h1 style=\"font-size:20px\">이 사이트는 비밀번호로 보호되어 있습니다</h1>{msg}"
        '<form method="post"><input type="password" name="password" autofocus required style="width:100%;padding:8px;margin:8px 0">'
        '<button type="submit" style="padding:8px 16px">열기</button></form></body></html>'
    )
