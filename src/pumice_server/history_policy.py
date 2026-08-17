"""Pure path-classification helper, deliberately kept free of any pb2-generated imports (see
PLUGIN_BOOKMARK_HISTORY_EXCLUSION_구현_계획.md's "왜 service.py에 바로 안 넣고 새 모듈로 뺐나" for why
this can't just live in service.py -- importing that module outside the real server process,
e.g. from a test, fails without main.py's sys.path workaround).
"""


def is_history_excluded_path(rel_path: str) -> bool:
    """Plugin files and bookmarks.json are machine-managed state (a plugin's own build output,
    or Obsidian's own bookmark list) rather than user-authored content -- there's no "restore
    an old version" use case for either, so unlike every other synced file they should never get
    a permanent full-copy backup or a history row. Their current content still gets stored and
    synced normally; this only gates whether finalize_pushed_file()/finalize_deduped_file()/
    _scan_and_merge() also create a backup."""
    return rel_path.startswith(".obsidian/plugins/") or rel_path == ".obsidian/bookmarks.json"
