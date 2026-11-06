"""PoC integration tests for the opt-in Quartz renderer (quartz_renderer.py + the hooks in web.py).

These drive the REAL view functions (publish_view / custom_domain_view / publish_upload_view /
publish_remove_view) against a real SQLite repository, real files under tmp_path, and a REAL Quartz
build. Needs a Quartz 5 checkout with node_modules installed:

    PUMICE_QUARTZ_DIR=/path/to/quartz-checkout uv run pytest tests/test_quartz_renderer.py

Without it (or without `node`) the Quartz-dependent tests are skipped; the scheduler/CSS/pure-logic
tests always run. Same fake-request style as test_publish_views.py.
"""

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from pumice_server import plugin_loader, quartz_renderer, web
from pumice_server.repository import SqlAlchemyMetadataRepository
from test_publish_views import FakeRequest, upload

QUARTZ_DIR = os.environ.get("PUMICE_QUARTZ_DIR")
needs_quartz = pytest.mark.skipif(
    not QUARTZ_DIR or not shutil.which("node"), reason="PUMICE_QUARTZ_DIR (Quartz checkout) and node are required"
)
DOCS_EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"
BASE = "/publish/alice/vault1/"


class QReq(FakeRequest):
    """FakeRequest + the few attributes the Quartz branch reads (cookies, POST params)."""

    def __init__(self, *a, cookies=None, **kw):
        super().__init__(*a, **kw)
        self.cookies = cookies or {}


def get(site, path, *, cookies=None, method="GET", params=None, domain=None, headers=None, scheme="https"):
    if domain:
        req = QReq(site.repo, site.data_dir, matchdict={"path": path}, domain=domain, host=domain,
                   cookies=cookies, method=method, params=params, headers=headers, scheme=scheme)
        req.registry.settings.update(site.settings)
        return web.custom_domain_view(req)
    req = QReq(site.repo, site.data_dir, matchdict={"username": "alice", "vault_id": getattr(site, "vault", "vault1"), "path": path},
               cookies=cookies, method=method, params=params, headers=headers, scheme=scheme)
    req.registry.settings.update(site.settings)
    return web.publish_view(req)


def text(resp):
    return resp.body.decode("utf-8")


class Site:
    pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    if not QUARTZ_DIR or not shutil.which("node"):
        pytest.skip("Quartz checkout / node not available")
    s = Site()
    s.data_dir = tmp_path_factory.mktemp("data")
    s.repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    s.repo.init_db()
    s.settings = {"data_dir": str(s.data_dir), "quartz_dir": QUARTZ_DIR, "publish_password_secret": "test-secret", "quartz_debounce_seconds": 0.05}

    up = lambda path, content, **kw: upload(s.repo, s.data_dir, "alice", "vault1", path, content, **kw)
    up("index.md", "---\ntitle: 홈\n---\n# 홈\n[[노트1]] 와 [[폴더A/노트2|별칭표시]] 와 [[없는노트]] #태그1\n".encode())
    up("노트1.md", ("---\ntitle: 노트1\naliases: [첫노트, note-one]\npermalink: my-first\ntags: [a, b]\n---\n"
                    "> [!note] 콜아웃\n> 내용\n\n수식 $E=mc^2$\n\n[[폴더A/노트2#섹션]] ![[assets/pic.png]]\n").encode(),
       permalink="my-first", aliases=["첫노트", "note-one"])
    up("폴더A/노트2.md", "## 섹션\n본문 [[노트1]]\n".encode())
    up("폴더A/하위/노트3.md", "# 노트3\n[[노트1]]\n".encode())
    up("assets/pic.png", b"\x89PNG\r\n\x1a\n")
    up("publish.css", (DOCS_EXAMPLES / "publish.css.example").read_bytes())
    up("publish.js", (DOCS_EXAMPLES / "publish.js.example").read_bytes())

    quartz_renderer.set_vault_renderer(str(s.data_dir), "alice", "vault1", "quartz")
    started = time.monotonic()
    web._run_quartz_build_job(("alice", "vault1"), {"repository": s.repo, "settings": s.settings})
    s.build_seconds = time.monotonic() - started
    s.out_dir = quartz_renderer.site_out_dir(str(s.data_dir), "alice", "vault1")
    return s


@pytest.fixture
def passwords(site):
    path = Path(site.data_dir) / "publish_meta" / "alice" / "vault1" / "passwords.json"
    path.write_text(json.dumps([{"name": "team", "pw": "s3cret"}]), encoding="utf-8")
    yield
    path.unlink()


@pytest.fixture
def custom_domain(site):
    def _set(redirect=False):
        site.repo.set_custom_domain("alice", "vault1", "docs.example.com", redirect=redirect)
    yield _set
    site.repo.set_custom_domain("alice", "vault1", "")


# ─── 1. serving under /publish/{user}/{vault}/ ─────────────────────────────────────────────────

@needs_quartz
def test_site_root_is_served_with_relative_links(site):
    resp = get(site, "")
    assert resp.status_code == 200
    html = text(resp)
    assert 'href="./노트1"' in html and 'href="./폴더a/노트2"' in html


@needs_quartz
def test_build_assets_are_served_with_correct_content_types(site):
    css = next(p.name for p in Path(site.out_dir).glob("index-*.css"))
    resp = get(site, css)
    assert resp.status_code == 200 and resp.content_type == "text/css"
    pic = get(site, "assets/pic.png")
    assert pic.status_code == 200 and pic.content_type == "image/png"


@needs_quartz
def test_nested_page_links_resolve_against_its_own_url(site):
    html = text(get(site, "폴더a/하위/노트3"))
    # depth-relative: 2 levels up to reach 노트1 -- correct ONLY if served at its own URL depth
    assert 'href="../../노트1"' in html


@needs_quartz
def test_path_traversal_is_rejected(site):
    assert get(site, "../../../etc/passwd").status_code == 403


# ─── 2. permalink / alias / slug ───────────────────────────────────────────────────────────────

@needs_quartz
def test_permalink_redirects_to_the_canonical_slug(site):
    resp = get(site, "my-first")
    assert resp.status_code == 302 and resp.location == BASE + "%EB%85%B8%ED%8A%B81"


@needs_quartz
def test_alias_redirects_to_the_canonical_slug(site):
    for alias in ("첫노트", "note-one"):
        resp = get(site, alias)
        assert resp.status_code == 302 and resp.location == BASE + "%EB%85%B8%ED%8A%B81", alias


@needs_quartz
def test_existing_case_preserved_url_still_works_via_redirect(site):
    # Quartz lowercases folder slugs (폴더A -> 폴더a); the pre-existing URL must keep resolving.
    for old in ("폴더A/노트2.md", "폴더A/노트2"):
        resp = get(site, old)
        assert resp.status_code == 302 and resp.location.endswith("%ED%8F%B4%EB%8D%94a/%EB%85%B8%ED%8A%B82"), old
    assert get(site, "폴더a/노트2").status_code == 200


@needs_quartz
def test_unknown_page_is_404(site):
    assert get(site, "없는페이지").status_code == 404


# ─── 3. password gate ──────────────────────────────────────────────────────────────────────────

@needs_quartz
def test_unprotected_site_is_open(site):
    assert get(site, "").status_code == 200


@needs_quartz
def test_protected_site_shows_login_without_cookie(site, passwords):
    resp = get(site, "")
    assert resp.status_code == 401 and "password" in text(resp)
    assert get(site, "assets/pic.png").status_code == 401  # assets are gated too


@needs_quartz
def test_wrong_password_is_rejected_and_right_one_sets_a_cookie(site, passwords):
    assert get(site, "", method="POST", params={"password": "nope"}).status_code == 401
    ok = get(site, "노트1", method="POST", params={"password": "s3cret"})
    assert ok.status_code == 303 and ok.location == BASE + "%EB%85%B8%ED%8A%B81"
    set_cookie = ok.headers["Set-Cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie and "secure" in set_cookie
    name, _, value = set_cookie.split(";")[0].partition("=")
    assert get(site, "", cookies={name: value}).status_code == 200


@needs_quartz
def test_tampered_or_stale_cookies_are_rejected(site, passwords):
    name = quartz_renderer.cookie_name("alice", "vault1")
    good = quartz_renderer.make_cookie("test-secret", "alice", "vault1", [{"name": "team", "pw": "s3cret"}])
    assert get(site, "", cookies={name: good[:-1] + ("0" if good[-1] != "0" else "1")}).status_code == 401
    old = quartz_renderer.make_cookie("test-secret", "alice", "vault1", [{"name": "team", "pw": "s3cret"}], now=time.time() - 8 * 24 * 3600)
    assert get(site, "", cookies={name: old}).status_code == 401
    # rotating the password invalidates previously issued cookies
    other = quartz_renderer.make_cookie("test-secret", "alice", "vault1", [{"name": "team", "pw": "changed"}])
    assert get(site, "", cookies={name: other}).status_code == 401


@needs_quartz
def test_protection_without_a_configured_secret_fails_closed(site, passwords, monkeypatch):
    monkeypatch.delenv("PUBLISH_PASSWORD_SECRET", raising=False)
    saved = site.settings.pop("publish_password_secret")
    try:
        assert get(site, "").status_code == 503
    finally:
        site.settings["publish_password_secret"] = saved


# ─── 4. custom domain & redirect ───────────────────────────────────────────────────────────────

@needs_quartz
def test_custom_domain_serves_the_same_build(site, custom_domain):
    custom_domain()
    resp = get(site, "", domain="docs.example.com")
    assert resp.status_code == 200 and 'href="./노트1"' in text(resp)


@needs_quartz
def test_old_url_301s_to_custom_domain_when_redirect_enabled(site, custom_domain):
    custom_domain(redirect=True)
    resp = get(site, "노트1")
    assert resp.status_code == 301 and resp.location == "https://docs.example.com/%EB%85%B8%ED%8A%B81"
    assert get(site, "노트1", domain="docs.example.com").status_code == 200


@needs_quartz
def test_alias_redirect_on_a_custom_domain_stays_on_that_domain(site, custom_domain):
    custom_domain()
    resp = get(site, "첫노트", domain="docs.example.com")
    assert resp.status_code == 302 and resp.location == "https://docs.example.com/%EB%85%B8%ED%8A%B81"


@needs_quartz
def test_reverse_proxy_header_still_selects_the_vault(site, custom_domain):
    custom_domain()
    req = QReq(site.repo, site.data_dir, domain="internal-proxy", headers={"x-obsidian-custom-domain": "docs.example.com"})
    assert web.custom_domain_route_predicate(None, req) is True


# ─── 5. publish.css ────────────────────────────────────────────────────────────────────────────

@needs_quartz
def test_publish_css_is_the_last_thing_in_head(site):
    html = text(get(site, ""))
    head = html[: html.index("</head>")]
    assert head.endswith(f'<link rel="stylesheet" href="{BASE}publish.css">')


@needs_quartz
def test_publish_css_theme_variables_are_mapped_to_quartz_variables(site):
    css = text(get(site, "publish.css"))
    assert "--accent-blue:#c2410c;--secondary:#c2410c" in css.replace(" ", "")
    assert "--heading-color:#7c2d12;--dark:#7c2d12" in css.replace(" ", "")
    assert '[saved-theme="dark"]' in css and "data-theme" not in css


# ─── 6. publish.js ─────────────────────────────────────────────────────────────────────────────

@needs_quartz
def test_publish_js_is_ignored_without_a_custom_domain_but_still_served_as_an_asset(site):
    assert "publish.js" not in text(get(site, ""))
    assert get(site, "publish.js").status_code == 200


@needs_quartz
def test_publish_js_loads_on_every_url_once_a_custom_domain_exists(site, custom_domain):
    custom_domain()
    for resp in (get(site, ""), get(site, "", domain="docs.example.com")):  # old path AND custom host
        html = text(resp)
        assert '<script src="https://docs.example.com/publish.js" defer></script>' in html
        assert "window.pumicePreload=" in html and "pumice-note-root" in html


# ─── 7. server plugins (pre-build markdown transform) ──────────────────────────────────────────

@needs_quartz
def test_server_plugins_transform_note_bodies_before_the_build(tmp_path, monkeypatch):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    seen = []

    def good(content, *, owner_username, vault_id, config_entries):
        seen.append(content)
        return content.replace("{{hello}}", "HELLO-FROM-PLUGIN")

    def broken(content, **kw):
        raise RuntimeError("plugin bug")

    monkeypatch.setattr(plugin_loader, "load_plugin_entry_point", lambda pid: {"a-broken": broken, "b-good": good}[pid])
    upload(repo, tmp_path, "alice", "pv", "a.md", "---\ntitle: 제목A\n---\n본문 {{hello}} 끝 [[b]]\n".encode(), permalink="pretty-a")
    upload(repo, tmp_path, "alice", "pv", "b.md", b"# B\n[[a]]\n")
    for pid in ("a-broken", "b-good"):
        repo.enable_plugin("alice", "pv", pid, 1)

    settings = {"data_dir": str(tmp_path), "quartz_dir": QUARTZ_DIR}
    web._run_quartz_build_job(("alice", "pv"), {"repository": repo, "settings": settings})

    html = (Path(quartz_renderer.site_out_dir(str(tmp_path), "alice", "pv")) / "a.html").read_text(encoding="utf-8")
    assert "HELLO-FROM-PLUGIN" in html and "{{hello}}" not in html  # broken plugin skipped, good one applied
    assert "제목A" in html  # frontmatter survived the transform
    a_seen = next(s for s in seen if "{{hello}}" in s)
    assert not a_seen.lstrip().startswith("---")  # plugin never sees frontmatter
    assert "[[" not in a_seen and '<a href="b" class="wiki-link">b</a>' in a_seen  # wikilinks already resolved, like today
    idx = json.loads((Path(quartz_renderer.site_out_dir(str(tmp_path), "alice", "pv")) / "static" / "contentIndex.json").read_text(encoding="utf-8"))
    assert "b" in idx["a"]["links"]  # graph/backlink edge survives in the plugin-touched note
    assert "a" in idx["b"]["links"]  # untouched note keeps native wikilink handling


@needs_quartz
def test_real_immich_plugin_end_to_end(tmp_path, monkeypatch):
    """The real pumice-plugin-immich (entry point, encrypted per-vault config, HTTP fetch) against a
    fake Immich server, through the real build."""
    import base64
    import threading as th
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pumice_server.plugin_config_crypto import encrypt_json

    monkeypatch.setenv("PLUGIN_CONFIG_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(300_000)

    class Immich(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("x-api-key") != "tok" or not self.path.startswith("/api/assets/asset-1/original"):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Immich)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
        repo.init_db()
        up = lambda p, c: upload(repo, tmp_path, "alice", "pv", p, c.encode())
        up("photo.md", "# 사진\n![immich:prof1/asset-1|여행 사진]\n\n![immich:nocfg/asset-1|설정없음]\n\n[[other]]\n")
        up("plain.md", "# 일반\n[[other]] [[photo]]\n")
        up("other.md", "# 다른\n")
        repo.enable_plugin("alice", "pv", "pumice-plugin-immich", 1)
        repo.set_plugin_config_entry("alice", "pv", "pumice-plugin-immich", "prof1",
                                     encrypt_json({"server_url": f"http://127.0.0.1:{srv.server_port}", "token": "tok"}), 1)
        settings = {"data_dir": str(tmp_path), "quartz_dir": QUARTZ_DIR}
        web._run_quartz_build_job(("alice", "pv"), {"repository": repo, "settings": settings})
    finally:
        srv.shutdown()

    out = Path(quartz_renderer.site_out_dir(str(tmp_path), "alice", "pv"))
    photo = (out / "photo.html").read_text(encoding="utf-8")
    assert 'src="data:image/png;base64,' in photo and "여행 사진" in photo   # resolved, fully self-contained
    assert "![immich:nocfg/asset-1|설정없음]" in photo or "immich:nocfg/asset-1" in photo  # unconfigured profile degrades to the original syntax
    idx = json.loads((out / "static" / "contentIndex.json").read_text(encoding="utf-8"))
    assert "other" in idx["photo"]["links"]                      # edge survives in the plugin-touched note
    assert {"photo", "other"} <= set(idx["plain"]["links"])      # untouched note: native wikilinks intact
    assert (out / "static" / "contentIndex.json").stat().st_size < 50_000  # base64 is not leaked into the search index


# ─── publish_options -> Quartz ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ov(tmp_path_factory):
    """A vault with NO index.md, mixed-case asset folders, favicon, notes with description/image, built
    with the build-time options (siteName / strictLineBreaks / slidingWindowMode) turned on."""
    if not QUARTZ_DIR or not shutil.which("node"):
        pytest.skip("Quartz checkout / node not available")
    s = Site()
    s.vault = "ov"
    s.data_dir = tmp_path_factory.mktemp("ovdata")
    s.repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    s.repo.init_db()
    s.settings = {"data_dir": str(s.data_dir), "quartz_dir": QUARTZ_DIR}
    up = lambda path, content, **kw: upload(s.repo, s.data_dir, "alice", "ov", path, content, **kw)
    up("Home.md", "---\ntitle: Home\n---\n# Home\n첫줄\n둘째줄 [[Top]]\n".encode())
    up("Top.md", "# Top\n본문\n".encode(), description="노트 고유 설명", image="Img/Logo.PNG")
    up("폴더A/Note.md", "# Note\n[[Home]]\n".encode())
    up("주제3.md", "# 주제3\n".encode())
    up("Img/Logo.PNG", b"\x89PNG\r\n\x1a\n")
    up("favicon-32.png", b"\x89PNG\r\n\x1a\n")
    quartz_renderer.set_vault_renderer(str(s.data_dir), "alice", "ov", "quartz")
    web.save_publish_meta(str(s.data_dir), "alice", "ov", "options.json",
                          {"siteName": "내 사이트", "strictLineBreaks": True, "slidingWindowMode": True})
    web._run_quartz_build_job(("alice", "ov"), {"repository": s.repo, "settings": s.settings})
    s.out_dir = quartz_renderer.site_out_dir(str(s.data_dir), "alice", "ov")
    return s


@pytest.fixture
def set_options(ov):
    """Request-time options only: changing these must NOT need a rebuild (the fixture never rebuilds)."""
    base = {"siteName": "내 사이트", "strictLineBreaks": True, "slidingWindowMode": True}

    def _set(**kw):
        web.save_publish_meta(str(ov.data_dir), "alice", "ov", "options.json", {**base, **kw})
    yield _set
    web.save_publish_meta(str(ov.data_dir), "alice", "ov", "options.json", base)


@needs_quartz
def test_vault_without_index_md_gets_a_root_listing_instead_of_a_404(ov):
    resp = get(ov, "")
    assert resp.status_code == 200
    html = text(resp)
    assert 'href="./home"' in html and 'href="./top"' in html and 'href="./폴더a/note"' in html


@needs_quartz
def test_build_time_options_siteName_line_breaks_and_sliding_window(ov):
    html = (Path(ov.out_dir) / "home.html").read_text(encoding="utf-8")
    assert ">내 사이트<" in html                      # siteName -> page title
    assert "첫줄<br" in html                         # strictLineBreaks=True -> hard line breaks (existing nl2br behaviour)
    assert "stacked" in html                         # slidingWindowMode=True -> stacked-pages plugin active


@needs_quartz
def test_unconfigured_options_mirror_existing_defaults_not_quartz_defaults(ov, set_options):
    set_options()  # nothing but the build-time keys
    html = text(get(ov, "home"))
    assert ".explorer{display:none}" in html          # showNavigation defaults to False (Quartz would show it)
    assert ".darkmode{display:none}" in html          # showThemeToggle defaults to False
    assert ".search{display:none}" not in html        # showSearch defaults to True
    assert ".article-title{display:none}" not in html and ".page{max-width:none}" not in html
    assert "setAttribute('saved-theme','dark')" in html  # defaultTheme defaults to dark
    assert "googletagmanager" not in html and 'name="robots"' not in html


@needs_quartz
def test_options_turned_on_are_applied_per_request_without_a_rebuild(ov, set_options):
    set_options(showNavigation=True, showThemeToggle=True, showSearch=False, defaultTheme="system",
                hideTitle=True, readableLineLength=False, noindex=True)
    resp = get(ov, "home")
    html = text(resp)
    assert ".explorer{display:none}" not in html and ".darkmode{display:none}" not in html
    assert ".search{display:none}" in html and ".article-title{display:none}" in html and ".page{max-width:none}" in html
    assert "setAttribute('saved-theme'" not in html   # "system" leaves Quartz's OS-preference behaviour alone
    assert '<meta name="robots" content="noindex">' in html and resp.headers["X-Robots-Tag"] == "noindex"
    assert get(ov, "assets/none").headers.get("X-Robots-Tag") == "noindex"  # also on 404s/assets


@needs_quartz
def test_google_analytics_only_runs_on_a_custom_domain(ov, set_options):
    set_options(googleAnalytics="G-ABC123")
    assert "googletagmanager" not in text(get(ov, "home"))
    ov.repo.set_custom_domain("alice", "ov", "docs.example.com")
    try:
        assert "googletagmanager.com/gtag/js?id=G-ABC123" in text(get(ov, "home"))
        set_options(googleAnalytics='G-1"><script>alert(1)</script>')
        assert "googletagmanager" not in text(get(ov, "home")) and "alert(1)" not in text(get(ov, "home"))  # id is validated
    finally:
        ov.repo.set_custom_domain("alice", "ov", "")


@needs_quartz
def test_navigation_ordering_and_hiding_are_handed_to_the_explorer_script(ov, set_options):
    set_options(showNavigation=True, navigationOrdering=["폴더A", "Top.md"], navigationHiddenItems=["주제3.md"])
    html = text(get(ov, "home"))
    cfg = json.loads(re.search(r"\}\)\((\{.*?\})\);</script>", html).group(1))
    assert cfg == {"order": ["d:폴더a", "f:top", "f:home"], "hidden": ["f:주제3"]}
    set_options(showNavigation=False, navigationOrdering=["폴더A"])
    assert "data-folderpath" not in text(get(ov, "home")).split("</head>")[0]  # no explorer script when navigation is off


@needs_quartz
def test_logo_favicon_and_original_asset_urls_survive_quartz_lowercasing(ov, set_options):
    set_options(logo="Img/Logo.PNG")
    html = text(get(ov, "home"))
    assert f"url('{BASE.replace('vault1', 'ov')}img/logo.PNG')" in html           # logo resolved to the BUILT path
    assert f'<link rel="icon" href="{BASE.replace("vault1", "ov")}favicon-32.png" sizes="32x32">' in html
    assert get(ov, "Img/Logo.PNG").status_code == 200                              # original URL still works
    assert get(ov, "Img/Logo.PNG").content_type == "image/png"


@needs_quartz
def test_note_description_and_image_override_the_generated_meta(ov):
    html = text(get(ov, "top"))
    for key in ("description", "og:description", "twitter:description"):
        assert re.search(rf'<meta[^>]*"{re.escape(key)}"[^>]*content="노트 고유 설명"', html), key
    for key in ("og:image", "og:image:url", "twitter:image"):
        assert f'content="{BASE.replace("vault1", "ov")}img/logo.PNG"' in html, key
    # a note without its own description keeps Quartz's generated one
    assert "노트 고유 설명" not in text(get(ov, "home"))


@needs_quartz
def test_index_file_option_redirects_the_root_and_ignores_an_unpublished_target(ov, set_options):
    set_options(indexFile="Top.md")
    resp = get(ov, "")
    assert resp.status_code == 302 and resp.location == BASE.replace("vault1", "ov") + "top"
    set_options(indexFile="없는파일.md")
    assert get(ov, "").status_code == 200  # falls back to the root listing, as the existing renderer does


@needs_quartz
def test_no_third_party_tracking_or_font_requests_in_built_pages(ov):
    hosts = set()
    for page in Path(ov.out_dir).rglob("*.html"):
        for m in re.finditer(r'<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)="(https?://[^/"]+)', page.read_text(encoding="utf-8")):
            hosts.add(m.group(1))
    hosts = {h for h in hosts if "localhost" not in h}
    assert not {h for h in hosts if "plausible" in h or "fonts.g" in h}, hosts
    assert hosts <= {"https://cdn.jsdelivr.net"}, hosts  # KaTeX only -- documented remaining external dependency


def test_options_view_schedules_a_rebuild_only_for_build_time_keys(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    quartz_renderer.set_vault_renderer(str(tmp_path), "alice", "vault1", "quartz")
    sched = RecordingScheduler()

    def save(options):
        req = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", json_body={"options": options})
        req.registry.settings["_quartz_scheduler"] = sched
        assert "options" in web.publish_options_view(req)

    save({"showNavigation": True, "defaultTheme": "light", "noindex": True, "logo": "a.png"})
    assert sched.calls == []                              # request-time options: no rebuild
    save({"siteName": "새 이름"})
    save({"siteName": "새 이름"})                          # unchanged value: nothing to rebuild
    save({"strictLineBreaks": True})
    save({"slidingWindowMode": True})
    assert sched.calls == [("alice", "vault1")] * 3


def test_navigation_order_matches_the_existing_navigation_tree():
    files = [{"path": p} for p in ("Home.md", "Top.md", "폴더A/Note.md", "폴더A/Sub/Deep.md", "주제3.md", "b.md")]
    for ordering, hidden in [([], []), (["폴더A", "Top.md"], ["주제3.md"]), (["b.md", "없는것", "Home.md"], ["폴더A"])]:
        expected = [e["display"] for e in web._build_navigation_tree(files, ordering, hidden, "/")]
        order, hid = quartz_renderer.navigation_order([f["path"] for f in files], ordering, hidden)
        assert [n[:-3] if n.endswith(".md") else n for n in order] == expected, (ordering, hidden)
        assert set(hid) == set(hidden) & {n.split("/")[0] for n in (f["path"] for f in files)}


def test_options_helpers_are_defensive():
    css = quartz_renderer.options_css({"showNavigation": True, "showSearch": True, "showThemeToggle": True, "readableLineLength": True}, "javascript:alert(1)')")
    assert css == ""                                      # a hostile logo URL is dropped, nothing else to emit
    assert quartz_renderer.theme_script("system") == "" and quartz_renderer.theme_script("bogus") == ""
    assert quartz_renderer.ga_snippet("") == "" and quartz_renderer.ga_snippet("G-1;evil()") == ""
    assert quartz_renderer.set_meta("<head></head>", "og:image", 'a"b') == '<head><meta property="og:image" content="a&quot;b"></head>'
    synth = quartz_renderer.synthetic_index_markdown('이름 "x"', ["b.md", "a/c.md"])
    assert 'title: "이름 \\"x\\""' in synth and "unlisted: true" in synth and synth.index("[[a/c]]") < synth.index("[[b]]")


# ─── 8. collaborators: rebuild must target the OWNER's vault ───────────────────────────────────

class RecordingScheduler:
    def __init__(self):
        self.calls = []

    def notify(self, key, payload=None, **kw):
        self.calls.append(key)
        self.last_kw = kw


def _upload_as(repo, tmp_path, scheduler, *, owner, vault_id, identity):
    headers = {"obs-path": "n.md", "obs-hash": "h"}
    req = FakeRequest(repo, tmp_path, owner=owner, vault_id=vault_id, headers=headers, body=b"# n", identity=identity)
    req.registry.settings["_quartz_scheduler"] = scheduler
    return web.publish_upload_view(req)


def test_collaborator_upload_triggers_a_rebuild_of_the_owners_vault(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    quartz_renderer.set_vault_renderer(str(tmp_path), "alice", "vault1", "quartz")
    sched = RecordingScheduler()
    assert _upload_as(repo, tmp_path, sched, owner="alice", vault_id="vault1", identity="bob") == {"ok": True}
    assert sched.calls == [("alice", "vault1")]  # never ("bob", ...)


def test_plugin_enable_disable_and_config_changes_schedule_a_rebuild(tmp_path, monkeypatch):
    import base64
    monkeypatch.setenv("PLUGIN_CONFIG_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    quartz_renderer.set_vault_renderer(str(tmp_path), "alice", "vault1", "quartz")
    sched = RecordingScheduler()
    pid = "pumice-plugin-immich"

    def call(view, body):
        req = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", json_body=body)
        req.registry.settings["_quartz_scheduler"] = sched
        assert view(req) == {"ok": True}

    call(web.publish_plugin_enable_view, {"plugin_id": pid})
    call(web.publish_plugin_config_save_view, {"plugin_id": pid, "entry_id": "p1", "values": {"server_url": "http://x", "token": "t"}})
    call(web.publish_plugin_config_delete_view, {"plugin_id": pid, "entry_id": "p1"})
    call(web.publish_plugin_disable_view, {"plugin_id": pid})
    assert sched.calls == [("alice", "vault1")] * 4


def test_vaults_not_opted_in_never_schedule_a_build(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    sched = RecordingScheduler()
    _upload_as(repo, tmp_path, sched, owner="alice", vault_id="vault1", identity="alice")
    assert sched.calls == []


def test_remove_also_schedules_a_rebuild(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    quartz_renderer.set_vault_renderer(str(tmp_path), "alice", "vault1", "quartz")
    sched = RecordingScheduler()
    _upload_as(repo, tmp_path, sched, owner="alice", vault_id="vault1", identity="alice")
    sched.calls.clear()
    req = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", json_body={"path": "n.md"})
    req.registry.settings["_quartz_scheduler"] = sched
    assert web.publish_remove_view(req) == {"ok": True}
    assert sched.calls == [("alice", "vault1")]


@needs_quartz
def test_upload_and_remove_drive_a_real_rebuild_end_to_end(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    upload(repo, tmp_path, "alice", "vault1", "a.md", b"# A")
    quartz_renderer.set_vault_renderer(str(tmp_path), "alice", "vault1", "quartz")
    settings = {"data_dir": str(tmp_path), "quartz_dir": QUARTZ_DIR, "quartz_debounce_seconds": 0.05}
    web._run_quartz_build_job(("alice", "vault1"), {"repository": repo, "settings": settings})

    def page(path):
        req = QReq(repo, tmp_path, matchdict={"username": "alice", "vault_id": "vault1", "path": path})
        return web.publish_view(req)

    assert page("b").status_code == 404

    up = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", headers={"obs-path": "b.md", "obs-hash": "h2"}, body=b"# B body")
    up.registry.settings.update(settings)
    assert web.publish_upload_view(up) == {"ok": True}
    assert up.registry.settings["_quartz_scheduler"].wait_idle(180)
    assert page("b").status_code == 200  # old site served until the new build swapped in, then b appears

    rm = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", json_body={"path": "b.md"})
    rm.registry.settings.update(settings)
    assert web.publish_remove_view(rm) == {"ok": True}
    assert rm.registry.settings["_quartz_scheduler"].wait_idle(180)
    assert page("b").status_code == 404


# ─── build safety / scheduler (no Quartz needed) ───────────────────────────────────────────────

def test_failed_build_leaves_the_live_site_untouched(tmp_path):
    out = tmp_path / "out"
    (out / "static").mkdir(parents=True)
    (out / "static" / "contentIndex.json").write_text("{}")
    (out / "index.html").write_text("LIVE")
    template = tmp_path / "tpl"
    template.mkdir()
    (template / "quartz.config.default.yaml").write_text("configuration: {}\nplugins: []\n")
    published = tmp_path / "pub"
    published.mkdir()
    (published / "a.md").write_text("# a")
    with pytest.raises(Exception):
        quartz_renderer.build_site(str(published), str(out), template_dir=str(template), base_url="x", page_title="t", timeout=60)
    assert (out / "index.html").read_text() == "LIVE"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".quartz-build-")]  # workspace cleaned


def test_scheduler_coalesces_bursts_into_one_build():
    calls = []
    sched = quartz_renderer.BuildScheduler(lambda key, payload: calls.append(key), debounce_seconds=0.05)
    for _ in range(10):
        sched.notify(("alice", "v"))
    assert sched.wait_idle(5)
    assert calls == [("alice", "v")]


def test_scheduler_runs_exactly_one_more_build_for_changes_during_a_build():
    started, release, calls = threading.Event(), threading.Event(), []

    def build(key, payload):
        calls.append(key)
        started.set()
        release.wait(5)

    sched = quartz_renderer.BuildScheduler(build, debounce_seconds=0.02)
    sched.notify(("alice", "v"))
    assert started.wait(5)
    sched.notify(("alice", "v"))  # arrives mid-build
    sched.notify(("alice", "v"))
    time.sleep(0.15)
    assert len(calls) == 1  # never two concurrent builds of one vault
    release.set()
    assert sched.wait_idle(5)
    assert len(calls) == 2


def test_scheduler_keeps_vaults_independent_and_survives_a_failing_build():
    calls = []

    def build(key, payload):
        calls.append(key)
        if key == ("a", "v"):
            raise RuntimeError("boom")

    sched = quartz_renderer.BuildScheduler(build, debounce_seconds=0.02)
    sched.notify(("a", "v"))
    sched.notify(("b", "v"))
    assert sched.wait_idle(5)
    assert sorted(calls) == [("a", "v"), ("b", "v")]


def test_split_frontmatter_roundtrip():
    fm, body = quartz_renderer.split_frontmatter("---\na: 1\n---\nbody\n")
    assert fm == "---\na: 1\n---\n" and body == "body\n"
    assert quartz_renderer.split_frontmatter("no frontmatter") == ("", "no frontmatter")


def test_config_disables_og_image_and_encrypted_pages_and_sets_base_url():
    import yaml
    default = (
        "configuration: {baseUrl: x, pageTitle: y, locale: en-US, analytics: {provider: plausible}, theme: {cdnCaching: true, fontOrigin: googleFonts}}\n"
        "plugins:\n- {source: '@q/og-image', enabled: true}\n- {source: '@q/encrypted-pages', enabled: true}\n- {source: '@q/graph', enabled: true}\n"
        "- {source: '@q/quartz-fonts', enabled: true}\n- {source: '@q/hard-line-breaks', enabled: false}\n- {source: '@q/stacked-pages', enabled: false}\n"
        "- {source: '@q/footer', enabled: true, options: {links: {GitHub: 'https://x'}}}\n"
    )
    cfg = yaml.safe_load(quartz_renderer.render_quartz_config(default, base_url="docs.example.com", page_title="T"))
    assert cfg["configuration"]["baseUrl"] == "docs.example.com" and cfg["configuration"]["locale"] == "ko-KR"
    on = {p["source"]: p["enabled"] for p in cfg["plugins"]}
    assert on == {"@q/og-image": False, "@q/encrypted-pages": False, "@q/graph": True, "@q/quartz-fonts": False,
                  "@q/hard-line-breaks": False, "@q/stacked-pages": False, "@q/footer": True}
    # third-party tracking and external font/CDN loading are off by default
    assert cfg["configuration"]["analytics"] is None
    assert cfg["configuration"]["theme"] == {"cdnCaching": False, "fontOrigin": "local"}
    assert next(p for p in cfg["plugins"] if p["source"].endswith("/footer"))["options"]["links"] == {}
    cfg2 = yaml.safe_load(quartz_renderer.render_quartz_config(default, base_url="x", page_title="T", strict_line_breaks=True, sliding_window=True))
    on2 = {p["source"]: p["enabled"] for p in cfg2["plugins"]}
    assert on2["@q/hard-line-breaks"] is True and on2["@q/stacked-pages"] is True


def test_publish_css_adapter_only_touches_mapped_variables():
    out = quartz_renderer.adapt_publish_css(':root[data-theme="dark"]{--accent-blue:#111;--code-bg:#222}.pumice-x{color:red}')
    assert "--accent-blue:#111;--secondary:#111" in out
    assert "--code-bg:#222" in out and "--code-bg:#222;--" not in out  # unmapped variable untouched
    assert ".pumice-x{color:red}" in out  # structural selectors are passed through unchanged (they just match nothing)


# ─── production wiring: status, recovery, renderer switch API, fallback, limits ─────────────────

from test_publish_views import render  # noqa: E402


def _quartz_vault(tmp_path, owner="alice", vault="v", *, built=False, state=None, renderer="quartz"):
    quartz_renderer.set_vault_renderer(str(tmp_path), owner, vault, renderer)
    if built:
        out = Path(quartz_renderer.site_out_dir(str(tmp_path), owner, vault)) / "static"
        out.mkdir(parents=True)
        (out / "contentIndex.json").write_text("{}")
    if state:
        quartz_renderer.write_status(str(tmp_path), owner, vault, state)


def test_status_roundtrip_and_default(tmp_path):
    assert quartz_renderer.read_status(str(tmp_path), "alice", "v") == {"state": None}
    quartz_renderer.write_status(str(tmp_path), "alice", "v", "failed", error="boom")
    got = quartz_renderer.read_status(str(tmp_path), "alice", "v")
    assert got["state"] == "failed" and got["error"] == "boom" and got["updated_at"] > 0


def test_recover_candidates_picks_only_vaults_that_need_a_build(tmp_path):
    _quartz_vault(tmp_path, vault="never-built")                              # no site yet
    _quartz_vault(tmp_path, vault="fine", built=True, state="ok")             # nothing to do
    _quartz_vault(tmp_path, vault="failed-before", built=True, state="failed")  # old site still serves; not retried blindly
    _quartz_vault(tmp_path, vault="interrupted", built=True, state="building")  # process died mid-build
    _quartz_vault(tmp_path, vault="queued", built=True, state="pending")
    _quartz_vault(tmp_path, vault="legacy", renderer=None)                    # not a Quartz vault
    got = quartz_renderer.recover_candidates(str(tmp_path))
    assert sorted(v for _, v in got) == ["interrupted", "never-built", "queued"]


def test_recover_quartz_sites_queues_immediately_and_marks_pending(tmp_path, monkeypatch):
    _quartz_vault(tmp_path, vault="a")
    _quartz_vault(tmp_path, vault="b", built=True, state="ok")
    monkeypatch.setattr(web, "_quartz_available", lambda s: True)
    sched = RecordingScheduler()
    settings = {"data_dir": str(tmp_path), "repository": object(), "_quartz_scheduler": sched}
    assert web.recover_quartz_sites(settings) == [("alice", "a")]
    assert sched.calls == [("alice", "a")] and sched.last_kw == {"immediate": True}
    assert quartz_renderer.read_status(str(tmp_path), "alice", "a")["state"] == "pending"


def test_recover_does_nothing_when_quartz_is_not_installed(tmp_path, monkeypatch):
    _quartz_vault(tmp_path, vault="a")
    monkeypatch.setattr(web, "_quartz_available", lambda s: False)
    sched = RecordingScheduler()
    assert web.recover_quartz_sites({"data_dir": str(tmp_path), "repository": object(), "_quartz_scheduler": sched}) == []
    assert sched.calls == []


def test_quartz_setting_precedence(monkeypatch):
    monkeypatch.setenv("X_ENV", "from-env")
    assert web._quartz_setting({"k": "from-settings"}, "k", "X_ENV", "d") == "from-settings"
    assert web._quartz_setting({}, "k", "X_ENV", "d") == "from-env"
    assert web._quartz_setting({"k": ""}, "k", "X_ENV", "d") == "from-env"
    monkeypatch.delenv("X_ENV")
    assert web._quartz_setting({}, "k", "X_ENV", "d") == "d"


def _renderer_call(repo, tmp_path, sched, method="GET", body=None, available=True, monkeypatch=None):
    if monkeypatch:
        monkeypatch.setattr(web, "_quartz_available", lambda s: available)
    req = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", json_body=body or {}, method=method)
    req.registry.settings["_quartz_scheduler"] = sched
    return web.publish_renderer_view(req)


def test_renderer_api_reports_the_default_state(tmp_path, monkeypatch):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    got = _renderer_call(repo, tmp_path, RecordingScheduler(), monkeypatch=monkeypatch, available=True)
    assert got == {"renderer": "default", "quartz_available": True, "site_ready": False, "build": {"state": None}}


def test_renderer_api_rejects_bad_input_and_an_unavailable_quartz(tmp_path, monkeypatch):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    assert _renderer_call(repo, tmp_path, RecordingScheduler(), "POST", {"renderer": "nope"}, monkeypatch=monkeypatch).status_code == 400
    sched = RecordingScheduler()
    resp = _renderer_call(repo, tmp_path, sched, "POST", {"renderer": "quartz"}, available=False, monkeypatch=monkeypatch)
    assert resp.status_code == 409 and sched.calls == []
    assert not quartz_renderer.vault_uses_quartz(str(tmp_path), "alice", "vault1")  # nothing was switched


def test_renderer_api_switches_to_quartz_queues_a_build_and_switches_back(tmp_path, monkeypatch):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    sched = RecordingScheduler()
    got = _renderer_call(repo, tmp_path, sched, "POST", {"renderer": "quartz"}, monkeypatch=monkeypatch)
    assert got["renderer"] == "quartz" and got["build"]["state"] == "pending" and got["site_ready"] is False
    assert sched.calls == [("alice", "vault1")] and sched.last_kw == {"immediate": True}
    back = _renderer_call(repo, tmp_path, sched, "POST", {"renderer": "default"}, monkeypatch=monkeypatch)
    assert back["renderer"] == "default" and len(sched.calls) == 1  # switching back never builds


def test_default_renderer_keeps_serving_until_the_first_quartz_build_exists(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    upload(repo, tmp_path, "alice", "vault1", "a.md", b"# A body")
    quartz_renderer.set_vault_renderer(str(tmp_path), "alice", "vault1", "quartz")  # switched, nothing built yet
    result = render(repo, tmp_path, "alice", "vault1", "a.md")
    assert isinstance(result, dict) and "A body" in result["content"]  # legacy page, no downtime


def test_a_protected_vault_is_never_served_unprotected_while_quartz_is_not_built(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    upload(repo, tmp_path, "alice", "vault1", "a.md", b"# secret body")
    quartz_renderer.set_vault_renderer(str(tmp_path), "alice", "vault1", "quartz")
    web.save_publish_meta(str(tmp_path), "alice", "vault1", "passwords.json", [{"name": "t", "pw": "pw"}])
    req = QReq(repo, tmp_path, matchdict={"username": "alice", "vault_id": "vault1", "path": "a.md"})
    req.registry.settings["publish_password_secret"] = "s"
    resp = web.publish_view(req)
    assert resp.status_code == 401 and b"secret body" not in resp.body       # login page, not the note
    cookie = quartz_renderer.make_cookie("s", "alice", "vault1", [{"name": "t", "pw": "pw"}])
    req2 = QReq(repo, tmp_path, matchdict={"username": "alice", "vault_id": "vault1", "path": "a.md"},
                cookies={quartz_renderer.cookie_name("alice", "vault1"): cookie})
    req2.registry.settings["publish_password_secret"] = "s"
    assert web.publish_view(req2).status_code == 503                          # authenticated, but nothing built yet


def test_failed_build_is_recorded_with_the_error_and_leaves_the_old_site(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    upload(repo, tmp_path, "alice", "v", "a.md", b"# A")
    _quartz_vault(tmp_path, vault="v", built=True, state="ok")
    settings = {"data_dir": str(tmp_path), "quartz_dir": str(tmp_path / "missing-checkout")}
    with pytest.raises(Exception):
        web._run_quartz_build_job(("alice", "v"), {"repository": repo, "settings": settings})
    status = quartz_renderer.read_status(str(tmp_path), "alice", "v")
    assert status["state"] == "failed" and status["error"] and status["finished_at"] >= status["started_at"]
    assert (Path(quartz_renderer.site_out_dir(str(tmp_path), "alice", "v")) / "static" / "contentIndex.json").exists()


def test_a_change_that_arrives_mid_build_keeps_the_vault_pending(tmp_path, monkeypatch):
    def build_that_gets_a_newer_change(key, payload):
        time.sleep(0.01)
        quartz_renderer.write_status(str(tmp_path), "alice", "v", "pending", requested_at=int(time.time() * 1000) + 5)
        return {"seconds": 1}

    monkeypatch.setattr(web, "_build_quartz_site", build_that_gets_a_newer_change)
    web._run_quartz_build_job(("alice", "v"), {"repository": None, "settings": {"data_dir": str(tmp_path)}})
    assert quartz_renderer.read_status(str(tmp_path), "alice", "v")["state"] == "pending"  # not overwritten by "ok"


def test_scheduler_never_exceeds_the_concurrent_build_limit():
    lock, active, peak = threading.Lock(), [0], [0]

    def build(key, payload):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        with lock:
            active[0] -= 1

    sched = quartz_renderer.BuildScheduler(build, debounce_seconds=0.01, max_concurrent=2)
    for i in range(6):
        sched.notify((f"u{i}", "v"))
    assert sched.wait_idle(15)
    assert peak[0] == 2  # six vaults at once, never more than two builds running


def test_scheduler_immediate_skips_the_debounce():
    started = threading.Event()
    sched = quartz_renderer.BuildScheduler(lambda k, p: started.set(), debounce_seconds=30)
    sched.notify(("a", "v"), immediate=True)
    assert started.wait(5)  # would take 30s with the debounce


@needs_quartz
def test_a_successful_real_build_records_ok_with_timing(site):
    status = quartz_renderer.read_status(str(site.data_dir), "alice", "vault1")
    assert status["state"] == "ok" and status["seconds"] > 0 and status["finished_at"] >= status["started_at"]


@needs_quartz
def test_switch_to_quartz_and_back_end_to_end_through_the_api(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    upload(repo, tmp_path, "alice", "vault1", "a.md", b"# A body")
    settings = {"data_dir": str(tmp_path), "quartz_dir": QUARTZ_DIR}

    def api(body=None, method="POST"):
        req = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", json_body=body or {}, method=method)
        req.registry.settings.update(settings)
        return req, web.publish_renderer_view(req)

    req, got = api({"renderer": "quartz"})
    assert got["renderer"] == "quartz" and got["build"]["state"] in ("pending", "building")
    assert isinstance(render(repo, tmp_path, "alice", "vault1", "a.md"), dict)          # legacy serves during the build
    assert req.registry.settings["_quartz_scheduler"].wait_idle(180)
    ready = web.publish_renderer_view(FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", method="GET"))
    assert ready["site_ready"] is True and ready["build"]["state"] == "ok"
    page = web.publish_view(QReq(repo, tmp_path, matchdict={"username": "alice", "vault_id": "vault1", "path": "a"}))
    assert not isinstance(page, dict) and page.status_code == 200 and b"quartz-root" in page.body  # now Quartz answers
    page = web.publish_view(QReq(repo, tmp_path, matchdict={"username": "alice", "vault_id": "vault1", "path": "a.md"}))
    assert page.status_code == 302 and page.location.endswith("/a")
    _, back = api({"renderer": "default"})
    assert back["renderer"] == "default"
    assert isinstance(render(repo, tmp_path, "alice", "vault1", "a.md"), dict)          # rolled back instantly
