"""ChangeNotifier is a pure pub/sub registry -- no reactor/timeout dependency at all (the WS
sync resource owns the heartbeat/timing concerns separately, see ws_sync_resource.py).
subscribe() registers a callback that fires on every matching notify_change() call until
unsubscribed, since one connection needs to receive many notifications over its lifetime
(unlike the discarded long-polling design's one-shot Deferred). See
#10_실시간_변경_알림_구현_계획.md.

2026-08 Obsidian core fidelity follow-up (see #11_websocket_동기화_프로토콜_설계.md and
pumice-server/llm-wiki/07-push-file-metadata.md): notify_change()/subscribe() now carry a
per-file metadata dict instead of a bare "something changed" signal -- every notify_change()
call and every subscribe() callback below was updated accordingly.
"""

from pumice_server.change_notifier import ChangeNotifier

FILE_META = {"path": "a.md", "modified_at_ms": 1000, "size_bytes": 5, "content_hash": "h1", "is_deleted": False}


def test_subscriber_is_called_on_matching_notify():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda meta: calls.append(meta))

    notifier.notify_change("alice", "vault1", FILE_META)

    assert calls == [FILE_META]


def test_unsubscribed_callback_is_not_called():
    notifier = ChangeNotifier()
    calls = []
    unsubscribe = notifier.subscribe("alice", "vault1", lambda meta: calls.append(meta))
    unsubscribe()

    notifier.notify_change("alice", "vault1", FILE_META)

    assert calls == []


def test_different_vault_is_not_notified():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda meta: calls.append(meta))

    notifier.notify_change("alice", "vault2", FILE_META)

    assert calls == []


def test_different_owner_is_not_notified():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda meta: calls.append(meta))

    notifier.notify_change("bob", "vault1", FILE_META)

    assert calls == []


def test_multiple_subscribers_on_same_key_all_fire():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda meta: calls.append("a"))
    notifier.subscribe("alice", "vault1", lambda meta: calls.append("b"))

    notifier.notify_change("alice", "vault1", FILE_META)

    assert sorted(calls) == ["a", "b"]


def test_subscriber_fires_again_on_repeated_notify_not_one_shot():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda meta: calls.append(meta["path"]))

    notifier.notify_change("alice", "vault1", FILE_META)
    notifier.notify_change("alice", "vault1", FILE_META)
    notifier.notify_change("alice", "vault1", FILE_META)

    assert calls == ["a.md", "a.md", "a.md"]


def test_notify_with_no_subscribers_does_not_raise():
    notifier = ChangeNotifier()
    notifier.notify_change("nobody", "novault", FILE_META)  # must not raise


def test_unsubscribing_one_of_several_leaves_the_others_intact():
    notifier = ChangeNotifier()
    calls = []
    unsubscribe_a = notifier.subscribe("alice", "vault1", lambda meta: calls.append("a"))
    notifier.subscribe("alice", "vault1", lambda meta: calls.append("b"))
    unsubscribe_a()

    notifier.notify_change("alice", "vault1", FILE_META)

    assert calls == ["b"]


def test_unsubscribing_twice_does_not_raise():
    notifier = ChangeNotifier()
    unsubscribe = notifier.subscribe("alice", "vault1", lambda meta: None)
    unsubscribe()
    unsubscribe()  # must not raise


def test_did_metadata_change_true_when_a_file_is_added():
    from pumice_server.change_notifier import did_metadata_change

    before = {}
    after = {"a.md": {"content_hash": "h1", "is_deleted": False}}
    assert did_metadata_change(before, after) is True


def test_did_metadata_change_true_when_content_hash_differs():
    from pumice_server.change_notifier import did_metadata_change

    before = {"a.md": {"content_hash": "h1", "is_deleted": False}}
    after = {"a.md": {"content_hash": "h2", "is_deleted": False}}
    assert did_metadata_change(before, after) is True


def test_did_metadata_change_true_when_a_file_becomes_deleted():
    from pumice_server.change_notifier import did_metadata_change

    before = {"a.md": {"content_hash": "h1", "is_deleted": False}}
    after = {"a.md": {"content_hash": "h1", "is_deleted": True}}
    assert did_metadata_change(before, after) is True


def test_did_metadata_change_false_when_nothing_changed():
    from pumice_server.change_notifier import did_metadata_change

    before = {"a.md": {"content_hash": "h1", "is_deleted": False}}
    after = {"a.md": {"content_hash": "h1", "is_deleted": False}}
    assert did_metadata_change(before, after) is False


def test_did_metadata_change_false_for_two_empty_vaults():
    from pumice_server.change_notifier import did_metadata_change

    assert did_metadata_change({}, {}) is False


def test_did_metadata_change_false_when_before_has_extra_db_row_keys():
    """Regression test: repository.load_all() returns full DB rows (owner_username, vault_id,
    path, plus the rest) for `before`, but _scan_and_merge() builds `after` with only 5 keys per
    file (no owner_username/vault_id) -- see service.py's _scan_and_merge(). A naive dict `!=`
    compares key sets too, so this mismatch alone made did_metadata_change() return True on every
    single Delta call regardless of whether any file actually changed, which meant every sync
    (including ones triggered purely by receiving a live-update notification) renotified itself,
    forming an infinite self-sustaining notify -> resync -> notify loop.
    """
    from pumice_server.change_notifier import did_metadata_change

    before = {
        "a.md": {
            "owner_username": "alice",
            "vault_id": "vault1",
            "path": "a.md",
            "content_hash": "h1",
            "is_deleted": False,
            "modified_at_ms": 1000,
            "size_bytes": 5,
        }
    }
    after = {
        "a.md": {
            "path": "a.md",
            "content_hash": "h1",
            "is_deleted": False,
            "modified_at_ms": 1000,
            "size_bytes": 5,
        }
    }
    assert did_metadata_change(before, after) is False


# ─── diff_changed_files (2026-08 push-metadata follow-up) ──────────────────────────────────


def test_diff_changed_files_includes_a_newly_added_file():
    from pumice_server.change_notifier import diff_changed_files

    before = {}
    after = {"a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False}}
    assert diff_changed_files(before, after) == after


def test_diff_changed_files_includes_a_file_whose_hash_changed():
    from pumice_server.change_notifier import diff_changed_files

    before = {"a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False}}
    after = {"a.md": {"path": "a.md", "content_hash": "h2", "is_deleted": False}}
    assert diff_changed_files(before, after) == after


def test_diff_changed_files_includes_a_file_that_became_deleted():
    from pumice_server.change_notifier import diff_changed_files

    before = {"a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False}}
    after = {"a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": True}}
    assert diff_changed_files(before, after) == after


def test_diff_changed_files_excludes_an_unchanged_file_but_keeps_a_changed_sibling():
    from pumice_server.change_notifier import diff_changed_files

    before = {
        "a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False},
        "b.md": {"path": "b.md", "content_hash": "hb", "is_deleted": False},
    }
    after = {
        "a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False},
        "b.md": {"path": "b.md", "content_hash": "hb2", "is_deleted": False},
    }
    assert diff_changed_files(before, after) == {"b.md": after["b.md"]}


def test_diff_changed_files_empty_when_nothing_changed():
    from pumice_server.change_notifier import diff_changed_files

    before = {"a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False}}
    after = {"a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False}}
    assert diff_changed_files(before, after) == {}


def test_diff_changed_files_ignores_the_db_row_key_set_mismatch_like_did_metadata_change():
    from pumice_server.change_notifier import diff_changed_files

    before = {"a.md": {"owner_username": "alice", "vault_id": "vault1", "path": "a.md", "content_hash": "h1", "is_deleted": False}}
    after = {"a.md": {"path": "a.md", "content_hash": "h1", "is_deleted": False}}
    assert diff_changed_files(before, after) == {}
