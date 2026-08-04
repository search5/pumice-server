"""ChangeNotifier is a pure pub/sub registry -- no reactor/timeout dependency at all (the SSE
resource owns the heartbeat/timing concerns separately, see watch_resource.py). subscribe()
registers a callback that fires on every matching notify_change() call until unsubscribed, since
one SSE connection needs to receive many notifications over its lifetime (unlike the discarded
long-polling design's one-shot Deferred). See #10_실시간_변경_알림_구현_계획.md.
"""

from pumice_server.change_notifier import ChangeNotifier


def test_subscriber_is_called_on_matching_notify():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda: calls.append(1))

    notifier.notify_change("alice", "vault1")

    assert calls == [1]


def test_unsubscribed_callback_is_not_called():
    notifier = ChangeNotifier()
    calls = []
    unsubscribe = notifier.subscribe("alice", "vault1", lambda: calls.append(1))
    unsubscribe()

    notifier.notify_change("alice", "vault1")

    assert calls == []


def test_different_vault_is_not_notified():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda: calls.append(1))

    notifier.notify_change("alice", "vault2")

    assert calls == []


def test_different_owner_is_not_notified():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda: calls.append(1))

    notifier.notify_change("bob", "vault1")

    assert calls == []


def test_multiple_subscribers_on_same_key_all_fire():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda: calls.append("a"))
    notifier.subscribe("alice", "vault1", lambda: calls.append("b"))

    notifier.notify_change("alice", "vault1")

    assert sorted(calls) == ["a", "b"]


def test_subscriber_fires_again_on_repeated_notify_not_one_shot():
    notifier = ChangeNotifier()
    calls = []
    notifier.subscribe("alice", "vault1", lambda: calls.append(1))

    notifier.notify_change("alice", "vault1")
    notifier.notify_change("alice", "vault1")
    notifier.notify_change("alice", "vault1")

    assert calls == [1, 1, 1]


def test_notify_with_no_subscribers_does_not_raise():
    notifier = ChangeNotifier()
    notifier.notify_change("nobody", "novault")  # must not raise


def test_unsubscribing_one_of_several_leaves_the_others_intact():
    notifier = ChangeNotifier()
    calls = []
    unsubscribe_a = notifier.subscribe("alice", "vault1", lambda: calls.append("a"))
    notifier.subscribe("alice", "vault1", lambda: calls.append("b"))
    unsubscribe_a()

    notifier.notify_change("alice", "vault1")

    assert calls == ["b"]


def test_unsubscribing_twice_does_not_raise():
    notifier = ChangeNotifier()
    unsubscribe = notifier.subscribe("alice", "vault1", lambda: None)
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
