"""Server-side merge of bookmarks.json / community-plugins.json on push (config_merge.py +
SyncServiceServicer.finalize_pushed_file's base_hash path).

Regression: two devices pushing at the same time used to overwrite each other (push_req carried no
base version), silently dropping one device's newly installed plugin."""

import hashlib
import json
import shutil

import pytest

from pumice_server.config_merge import is_server_merged_path, merge_config_bytes
from pumice_server.repository import SqlAlchemyMetadataRepository
from pumice_server.service import SyncServiceServicer

PLUGINS = ".obsidian/community-plugins.json"
BOOKMARKS = ".obsidian/bookmarks.json"


def dump(value) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_only_the_two_config_files_are_merged_paths():
    assert is_server_merged_path(PLUGINS)
    assert is_server_merged_path(BOOKMARKS)
    assert not is_server_merged_path(".obsidian/core-plugins.json")
    assert not is_server_merged_path("notes/todo.md")


# ─── pure merge ──────────────────────────────────────────────────────────────────────────────


def test_plugin_lists_from_two_devices_are_unioned():
    merged = merge_config_bytes(dump(["c1"]), ours=dump(["c1", "b1"]), theirs=dump(["c1", "a1"]))
    assert json.loads(merged) == ["c1", "a1", "b1"]


def test_a_plugin_removed_by_one_side_stays_removed():
    merged = merge_config_bytes(dump(["c1", "x"]), ours=dump(["c1", "x", "b1"]), theirs=dump(["c1"]))
    assert json.loads(merged) == ["c1", "b1"]


def test_missing_base_merges_as_a_union_without_deletions():
    assert json.loads(merge_config_bytes(None, dump(["b1"]), dump(["a1"]))) == ["a1", "b1"]


def test_bookmarks_entries_are_matched_by_identity_and_groups_merge_recursively():
    base = {"items": [{"type": "group", "title": "g", "items": [{"type": "file", "path": "x.md"}]}]}
    ours = {"items": [{"type": "group", "title": "g", "items": [{"type": "file", "path": "x.md"}, {"type": "file", "path": "b.md"}]}]}
    theirs = {"items": [{"type": "group", "title": "g", "items": [{"type": "file", "path": "x.md"}, {"type": "file", "path": "a.md"}]},
                        {"type": "file", "path": "top.md"}]}
    merged = json.loads(merge_config_bytes(dump(base), dump(ours), dump(theirs)))
    group = next(i for i in merged["items"] if i["type"] == "group")
    assert [i["path"] for i in group["items"]] == ["x.md", "a.md", "b.md"]
    assert any(i.get("path") == "top.md" for i in merged["items"])


def test_unparsable_input_returns_none_so_the_caller_keeps_the_upload():
    assert merge_config_bytes(None, b"not json", dump(["a"])) is None


def test_output_matches_obsidians_json_formatting():
    assert merge_config_bytes(dump([]), dump(["a"]), dump(["a"])) == b'[\n  "a"\n]'


# ─── finalize_pushed_file ────────────────────────────────────────────────────────────────────


@pytest.fixture
def servicer(tmp_path):
    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    return SyncServiceServicer(data_dir=str(tmp_path), repository=repo), repo, tmp_path


def push(servicer, content: bytes, mtime: int, base_hash: str = "", path: str = PLUGINS):
    return servicer.finalize_pushed_file(
        "alice", "vault1", path, content, len(content), mtime, sha(content), "dev", "user", base_hash,
    )


def stored(tmp_path, path: str = PLUGINS) -> bytes:
    return (tmp_path / "vaults" / "alice" / "vault1" / path).read_bytes()


def test_concurrent_installs_on_two_devices_keep_both_plugins(servicer):
    svc, repo, tmp_path = servicer
    c1 = dump(["c1"])
    assert push(svc, c1, 1000) == (True, "")

    # Device A and device B both started from c1, installed a1 / b1, and push in turn.
    assert push(svc, dump(["c1", "a1"]), 2000, base_hash=sha(c1)) == (True, "")
    assert push(svc, dump(["c1", "b1"]), 2500, base_hash=sha(c1)) == (True, "")

    assert json.loads(stored(tmp_path)) == ["c1", "a1", "b1"]
    meta = repo.load_one("alice", "vault1", PLUGINS)
    assert meta["content_hash"] == sha(stored(tmp_path))
    # Newer than both pushes, so each device sees "server is newer" and downloads the merge.
    assert meta["modified_at_ms"] > 2500


def test_fast_forward_push_is_stored_as_is(servicer):
    svc, repo, tmp_path = servicer
    c1 = dump(["c1"])
    push(svc, c1, 1000)
    new = dump(["c1", "a1"])
    push(svc, new, 2000, base_hash=sha(c1))
    assert stored(tmp_path) == new
    assert repo.load_one("alice", "vault1", PLUGINS)["modified_at_ms"] == 2000


def test_a_push_without_base_hash_still_overwrites_like_before(servicer):
    svc, _, tmp_path = servicer
    push(svc, dump(["c1", "a1"]), 1000)
    push(svc, dump(["c1", "b1"]), 2000)
    assert json.loads(stored(tmp_path)) == ["c1", "b1"]


def test_bookmarks_json_is_merged_even_though_it_has_no_history(servicer):
    svc, _, tmp_path = servicer
    base = dump({"items": [{"type": "file", "path": "x.md"}]})
    push(svc, base, 1000, path=BOOKMARKS)
    push(svc, dump({"items": [{"type": "file", "path": "x.md"}, {"type": "file", "path": "a.md"}]}), 2000,
         base_hash=sha(base), path=BOOKMARKS)
    push(svc, dump({"items": [{"type": "file", "path": "x.md"}, {"type": "file", "path": "b.md"}]}), 2500,
         base_hash=sha(base), path=BOOKMARKS)
    paths = [i["path"] for i in json.loads(stored(tmp_path, BOOKMARKS))["items"]]
    assert paths == ["x.md", "a.md", "b.md"]


def test_other_files_are_never_merged_even_with_a_base_hash(servicer):
    svc, _, tmp_path = servicer
    push(svc, b"one", 1000, path="note.md")
    push(svc, b"two", 2000, base_hash="whatever", path="note.md")
    assert stored(tmp_path, "note.md") == b"two"


def test_a_legacy_encrypted_copy_on_the_server_is_replaced_by_the_plaintext_upload(servicer):
    """Before the E2EE exemption these files were stored as ciphertext; the server can't parse
    that, so the first plaintext push simply wins and the file self-heals."""
    svc, _, tmp_path = servicer
    push(svc, b"\x00\x01ciphertext-not-json\xff", 1000)
    new = dump(["c1", "a1"])
    assert push(svc, new, 2000, base_hash="0") == (True, "")
    assert stored(tmp_path) == new


def test_a_deletion_survives_when_the_base_is_older_than_50_versions(servicer):
    svc, _, tmp_path = servicer
    base = dump(["c1", "x"])
    push(svc, base, 1000)
    for i in range(60):  # other devices keep pushing; the base would be pruned by a count-only limit
        push(svc, dump(["c1", "x", f"p{i}"]), 2000 + i, base_hash=sha(dump(["c1", "x"] + [f"p{j}" for j in range(i)])))
    # An offline device removed "x" from the base it synced long ago and now pushes.
    assert push(svc, dump(["c1"]), 9000, base_hash=sha(base)) == (True, "")
    result = json.loads(stored(tmp_path))
    assert "x" not in result and "p59" in result


def test_a_version_written_before_recording_existed_is_kept_as_base(servicer):
    svc, _, tmp_path = servicer
    old = dump(["c1", "x"])
    push(svc, old, 1000)
    shutil.rmtree(tmp_path / "merge_bases")  # as if it had been written by the pre-merge server
    # Device A fast-forwards from `old`; this must record `old` before replacing it.
    push(svc, dump(["c1", "x", "a1"]), 2000, base_hash=sha(old))
    # Device B, still on `old`, removed x and pushes.
    assert push(svc, dump(["c1"]), 3000, base_hash=sha(old)) == (True, "")
    assert json.loads(stored(tmp_path)) == ["c1", "a1"]


@pytest.mark.parametrize("first", ["A", "B"])
def test_two_devices_each_deleting_a_different_item_end_with_both_deleted(servicer, first):
    """Server has x1,y1,z1; A deletes x1 and B deletes y1 from the same base. Whoever pushes first,
    the result is only z1 -- for the plugin list and for bookmarks alike."""
    svc, _, tmp_path = servicer

    def file(name):
        return {"type": "file", "path": name}

    cases = [
        (PLUGINS, dump(["x1", "y1", "z1"]), dump(["y1", "z1"]), dump(["x1", "z1"]), lambda b: json.loads(b)),
        (BOOKMARKS, dump({"items": [file("x1"), file("y1"), file("z1")]}),
         dump({"items": [file("y1"), file("z1")]}), dump({"items": [file("x1"), file("z1")]}),
         lambda b: [i["path"] for i in json.loads(b)["items"]]),
    ]
    for path, base, a_version, b_version, read in cases:
        push(svc, base, 1000, path=path)
        pushes = [(a_version, 2000), (b_version, 2500)]
        if first == "B":
            pushes.reverse()
        for content, mtime in pushes:
            assert push(svc, content, mtime, base_hash=sha(base), path=path) == (True, "")
        assert read(stored(tmp_path, path)) == ["z1"]


def test_the_merged_result_is_broadcast_to_every_subscriber_including_the_uploader(tmp_path):
    """The ws `push` notification is what makes clients download the merge; it must carry the merged
    version's hash/mtime (not the uploaded one's), so the uploading device is told as well."""
    from pumice_server.change_notifier import ChangeNotifier

    repo = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    repo.init_db()
    notifier = ChangeNotifier()
    svc = SyncServiceServicer(data_dir=str(tmp_path), repository=repo, change_notifier=notifier)
    seen = []
    notifier.subscribe("alice", "vault1", seen.append)

    c1 = dump(["c1"])
    push(svc, c1, 1000)
    push(svc, dump(["c1", "a1"]), 2000, base_hash=sha(c1))
    seen.clear()
    push(svc, dump(["c1", "b1"]), 2500, base_hash=sha(c1))  # merged on the server

    merged = stored(tmp_path)
    assert json.loads(merged) == ["c1", "a1", "b1"]
    assert len(seen) == 1
    assert seen[0]["path"] == PLUGINS
    assert seen[0]["content_hash"] == sha(merged)
    assert seen[0]["modified_at_ms"] > 2500


def test_an_edit_made_on_the_uploaded_version_before_downloading_the_merge_keeps_its_deletions(servicer):
    svc, _, tmp_path = servicer
    base = dump(["c1", "x"])
    push(svc, base, 1000)
    push(svc, dump(["c1", "x", "a1"]), 2000, base_hash=sha(base))        # device A
    uploaded = dump(["c1", "x", "b1"])
    push(svc, uploaded, 2500, base_hash=sha(base))                       # device B -> merged on server
    assert json.loads(stored(tmp_path)) == ["c1", "x", "a1", "b1"]
    # B's last-synced version is what it uploaded; before downloading the merge it removes x.
    push(svc, dump(["c1", "b1"]), 3000, base_hash=sha(uploaded))
    assert json.loads(stored(tmp_path)) == ["c1", "a1", "b1"]
