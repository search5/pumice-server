"""TDD for metadata_cache.extract_metadata_cache_entry() -- the server-side half of
28_실제_아키텍처_전환_Phase1_노트본문.md (pumice repo)/27_실제_퍼블리시_사이트_아키텍처_조사.md.

Real Obsidian Publish serves a per-vault MetadataCache-shaped JSON blob (confirmed live via
curl against publish-01.obsidian.md, see 27_...md) that the browser uses to render note content,
outline, backlinks, etc. entirely client-side. This module reproduces that shape server-side from
a note's raw markdown so pumice-server's own client renderer (static/publish_render.js) can do the
same. The exact field names/pos-tuple format ([startLine, startCol, startOffset, endLine, endCol,
endOffset]) are confirmed against the real payload; the offset unit (python str index, not UTF-16
code units like real Obsidian's JS) is this implementation's own deliberate choice since the cache
here is only ever consumed by pumice's own client renderer, not real Obsidian's app.js.
"""

from pumice_server.metadata_cache import extract_metadata_cache_entry


def test_no_special_content_returns_empty_lists_and_no_frontmatter_keys():
    entry = extract_metadata_cache_entry("Just a plain paragraph.\n")
    assert entry["headings"] == []
    assert entry["links"] == []
    assert entry["listItems"] == []
    assert "frontmatter" not in entry
    assert "frontmatterPos" not in entry
    assert "frontmatterLinks" not in entry


def test_frontmatter_position_and_parsed_dict():
    md = "---\ntitle: Hello\ncount: 3\n---\nBody text.\n"
    entry = extract_metadata_cache_entry(md)
    assert entry["frontmatter"] == {"title": "Hello", "count": 3}
    # closing "---" is on line index 3 (0-indexed), 3 chars wide, ending right after it (no \n).
    start_offset = 0
    end_line = 3
    end_col = 3
    end_offset = md.index("---", 4) + 3
    assert entry["frontmatterPos"] == [0, 0, start_offset, end_line, end_col, end_offset]


def test_malformed_frontmatter_yaml_falls_back_to_empty_dict():
    md = "---\n: not valid: yaml: at all:\n---\nBody.\n"
    entry = extract_metadata_cache_entry(md)
    assert entry["frontmatter"] == {}


def test_frontmatter_links_extraction():
    md = '---\ncategory: "[[Core]]"\ntags:\n  - home\n---\nBody.\n'
    entry = extract_metadata_cache_entry(md)
    assert entry["frontmatterLinks"] == [
        {"link": "Core", "key": "category", "original": "[[Core]]", "displayText": "Core"}
    ]


def test_body_wikilink_inside_frontmatter_is_not_counted_as_a_body_link():
    md = '---\ncategory: "[[Core]]"\n---\nBody with [[Other]].\n'
    entry = extract_metadata_cache_entry(md)
    assert [l["link"] for l in entry["links"]] == ["Other"]


def test_headings_extraction_with_levels_and_positions():
    md = "# Title\n\nSome text.\n\n## Sub Heading\n"
    entry = extract_metadata_cache_entry(md)
    assert entry["headings"] == [
        {"heading": "Title", "level": 1, "pos": [0, 0, 0, 0, 7, 7]},
        {"heading": "Sub Heading", "level": 2, "pos": [4, 0, 21, 4, 14, 35]},
    ]


def test_headings_inside_fenced_code_block_are_skipped():
    md = "```\n# Not a heading\n```\n\n# Real heading\n"
    entry = extract_metadata_cache_entry(md)
    assert [h["heading"] for h in entry["headings"]] == ["Real heading"]


def test_links_extraction_with_and_without_alias():
    md = "See [[Target Note]] and [[Other|Custom Label]].\n"
    entry = extract_metadata_cache_entry(md)
    assert entry["links"] == [
        {"link": "Target Note", "displayText": "Target Note", "pos": [0, 4, 4, 0, 19, 19]},
        {"link": "Other", "displayText": "Custom Label", "pos": [0, 24, 24, 0, 46, 46]},
    ]


def test_links_inside_fenced_code_block_are_skipped():
    md = "```\n[[Not A Link]]\n```\n\n[[Real Link]]\n"
    entry = extract_metadata_cache_entry(md)
    assert [l["link"] for l in entry["links"]] == ["Real Link"]


def test_list_items_top_level_parent_is_negative_own_line():
    md = "- item one\n- item two\n"
    entry = extract_metadata_cache_entry(md)
    assert entry["listItems"] == [
        {"parent": 0, "pos": [0, 0, 0, 0, 10, 10]},
        {"parent": -1, "pos": [1, 0, 11, 1, 10, 21]},
    ]


def test_list_items_nested_parent_points_to_parent_line():
    md = "- parent\n  - child\n"
    entry = extract_metadata_cache_entry(md)
    assert entry["listItems"] == [
        {"parent": 0, "pos": [0, 0, 0, 0, 8, 8]},
        {"parent": 0, "pos": [1, 2, 11, 1, 9, 18]},
    ]


def test_list_items_two_separate_top_level_groups():
    md = "- a\n\n- b\n"
    entry = extract_metadata_cache_entry(md)
    assert [item["parent"] for item in entry["listItems"]] == [0, -2]
