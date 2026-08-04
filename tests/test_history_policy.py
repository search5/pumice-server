"""is_history_excluded_path() must return True for plugin files and bookmarks.json -- both are
machine-managed state (Obsidian's own or a plugin's build output), not user-authored content, so
keeping a permanent full-copy backup of every change (as _finalize_uploaded_file/_scan_and_merge
do for everything else) is pure storage waste with no restore use case. Everything else must keep
getting history as before. See PLUGIN_BOOKMARK_HISTORY_EXCLUSION_구현_계획.md.
"""

from pumice_server.history_policy import is_history_excluded_path


def test_plugin_file_is_excluded():
    assert is_history_excluded_path(".obsidian/plugins/foo/main.js") is True


def test_plugin_data_json_is_excluded():
    assert is_history_excluded_path(".obsidian/plugins/foo/data.json") is True


def test_nested_plugin_asset_is_excluded():
    assert is_history_excluded_path(".obsidian/plugins/foo/assets/icon.svg") is True


def test_bookmarks_json_is_excluded():
    assert is_history_excluded_path(".obsidian/bookmarks.json") is True


def test_regular_note_is_not_excluded():
    assert is_history_excluded_path("notes/a.md") is False


def test_plugins_directory_itself_is_not_excluded():
    # No trailing slash -- not a real file path in practice, but must not false-positive.
    assert is_history_excluded_path(".obsidian/plugins") is False


def test_path_merely_prefixed_by_bookmarks_json_is_not_excluded():
    # Exact match only, not startswith -- a real file that happens to share the prefix
    # (e.g. a conflict-backup copy) must still get normal history treatment.
    assert is_history_excluded_path(".obsidian/bookmarks.json.bak") is False
