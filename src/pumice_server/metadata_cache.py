"""Reproduces the shape of real Obsidian Publish's per-file MetadataCache export
(`/cache/{uid}`, confirmed live against publish-01.obsidian.md -- see
27_실제_퍼블리시_사이트_아키텍처_조사.md and 28_실제_아키텍처_전환_Phase1_노트본문.md, both in
the pumice repo) from a note's raw markdown, so pumice-server's own client renderer
(static/publish_render.js) can render notes the same way real Obsidian's app.js does: client-side,
from raw markdown + a preloaded metadata blob, instead of server-rendered HTML.

Field names and the position-tuple format ([startLine, startCol, startOffset, endLine, endCol,
endOffset], 0-indexed) are confirmed against the real payload. The offset unit is a deliberate
deviation: real Obsidian (a JS app) counts UTF-16 code units; this counts Python str (Unicode
codepoint) indices instead, since this cache is only ever consumed by pumice's own client
renderer, never real Obsidian's app.js -- server and client here just need to agree with each
other.

Known limitation carried over from -- and not unique to -- the real format: a top-level list
item's `parent` is encoded as the negative of its own start line, so a top-level item whose start
line is 0 serializes identically (`0`) to a *nested* item whose parent is line 0 -- integers have
no negative zero. Consumers must treat `parent == 0` as ambiguous only when line 0 is otherwise
plausible, e.g. by comparing against the item's own `pos` line.
"""

import re
from typing import Optional

import yaml

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HEADING_TRAILING_HASHES_RE = re.compile(r"\s+#+\s*$")
_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_LIST_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+")

_SEARCH_TEXT_MAX_LENGTH = 4000
_SEARCH_IMAGE_EMBED_RE = re.compile(r"!\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_SEARCH_HEADING_MARK_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_SEARCH_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_SEARCH_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_|`)")
_SEARCH_WHITESPACE_RE = re.compile(r"\s+")


def _plain_text_for_search(body: str) -> str:
    """Search index text (Phase 3, 28_실제_아키텍처_전환_Phase1_노트본문.md's follow-up) -- a
    lightweight own design, not reverse-engineered from real Obsidian (27_...md's curl sweep only
    confirmed /options, /cache and /access; no dedicated search endpoint was found, so how real
    Obsidian Publish's search actually works server-side is unconfirmed). This just needs to be
    good enough for client-side substring search: markdown links/wikilinks/embeds are reduced to
    their visible label, formatting markers are dropped, whitespace is collapsed, and the result
    is capped (own reasonable default, same category of guess as _auto_generate_description's
    200-char limit in web.py) so a very long note doesn't bloat metadata_cache.json."""
    text = _SEARCH_IMAGE_EMBED_RE.sub(lambda m: m.group(2) or m.group(1), body)
    text = _LINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    text = _SEARCH_MD_LINK_RE.sub(lambda m: m.group(1), text)
    text = _SEARCH_HEADING_MARK_RE.sub("", text)
    text = _SEARCH_EMPHASIS_RE.sub("", text)
    text = _SEARCH_WHITESPACE_RE.sub(" ", text).strip()
    return text[:_SEARCH_TEXT_MAX_LENGTH]


def _split_lines_with_offsets(text: str):
    lines = text.split("\n")
    offsets = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1
    return lines, offsets


def _extract_frontmatter(lines: list, offsets: list):
    """Returns (frontmatter_dict, pos_tuple, end_line_index_exclusive). end_line_index_exclusive
    is the first line *after* the closing `---`, used by callers to skip the frontmatter block
    when scanning for headings/links/list items -- a wikilink inside a frontmatter value belongs
    in frontmatterLinks, not the body links list."""
    if not lines or lines[0].strip() != "---":
        return {}, None, 0

    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_col = len(lines[i])
            pos = [0, 0, offsets[0], i, end_col, offsets[i] + end_col]
            yaml_text = "\n".join(lines[1:i])
            try:
                parsed = yaml.safe_load(yaml_text)
            except yaml.YAMLError:
                parsed = None
            frontmatter = parsed if isinstance(parsed, dict) else {}
            return frontmatter, pos, i + 1

    return {}, None, 0


def _extract_frontmatter_links(frontmatter: dict) -> list:
    links = []
    for key, value in frontmatter.items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            if not isinstance(v, str):
                continue
            for m in _LINK_RE.finditer(v):
                target = m.group(1).strip()
                display = m.group(2).strip() if m.group(2) else target
                links.append({
                    "link": target, "key": key, "original": m.group(0), "displayText": display,
                })
    return links


def _body_line_range(lines: list, start_line: int):
    """Yields (line_index, line_text) for lines usable for headings/links/list-item scanning --
    everything from start_line onward except the contents of fenced code blocks (a `# heading`-
    or `[[link]]`-looking line inside a ``` fence isn't really one)."""
    in_fence = False
    fence_marker = None
    for i in range(start_line, len(lines)):
        line = lines[i]
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(2)[0]
            if not in_fence:
                in_fence, fence_marker = True, marker
                continue
            elif marker == fence_marker:
                in_fence, fence_marker = False, None
                continue
        if in_fence:
            continue
        yield i, line


def _extract_headings(lines: list, offsets: list, start_line: int) -> list:
    headings = []
    for i, line in _body_line_range(lines, start_line):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        text = _HEADING_TRAILING_HASHES_RE.sub("", m.group(2).rstrip())
        end_col = len(line)
        headings.append({
            "heading": text, "level": level,
            "pos": [i, 0, offsets[i], i, end_col, offsets[i] + end_col],
        })
    return headings


def _extract_links(lines: list, offsets: list, start_line: int) -> list:
    links = []
    for i, line in _body_line_range(lines, start_line):
        for m in _LINK_RE.finditer(line):
            target = m.group(1).strip()
            display = m.group(2).strip() if m.group(2) else target
            start_col, end_col = m.start(), m.end()
            links.append({
                "link": target, "displayText": display,
                "pos": [i, start_col, offsets[i] + start_col, i, end_col, offsets[i] + end_col],
            })
    return links


def _extract_list_items(lines: list, offsets: list, start_line: int) -> list:
    items = []
    stack = []  # [(indent, line_index), ...] innermost last
    for i, line in _body_line_range(lines, start_line):
        m = _LIST_ITEM_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else -i
        stack.append((indent, i))
        end_col = len(line)
        items.append({
            "parent": parent,
            "pos": [i, indent, offsets[i] + indent, i, end_col, offsets[i] + end_col],
        })
    return items


def extract_metadata_cache_entry(raw_md: str) -> dict:
    lines, offsets = _split_lines_with_offsets(raw_md)
    frontmatter, frontmatter_pos, body_start = _extract_frontmatter(lines, offsets)

    entry = {
        "headings": _extract_headings(lines, offsets, body_start),
        "links": _extract_links(lines, offsets, body_start),
        "listItems": _extract_list_items(lines, offsets, body_start),
        "searchText": _plain_text_for_search("\n".join(lines[body_start:])),
    }
    if frontmatter_pos is not None:
        entry["frontmatter"] = frontmatter
        entry["frontmatterPos"] = frontmatter_pos
        entry["frontmatterLinks"] = _extract_frontmatter_links(frontmatter)
    return entry
