import os

from pumice_server.plugin_resolve_disk_cache import make_disk_cache


def test_cache_get_returns_none_for_a_missing_key(tmp_path):
    cache_get, _ = make_disk_cache(str(tmp_path))

    assert cache_get("no-such-key") is None


def test_cache_set_then_get_returns_the_value(tmp_path):
    cache_get, cache_set = make_disk_cache(str(tmp_path))

    cache_set("abc123", "<img src=\"data:image/png;base64,xyz\">")

    assert cache_get("abc123") == "<img src=\"data:image/png;base64,xyz\">"


def test_cache_set_overwrites_an_existing_key(tmp_path):
    cache_get, cache_set = make_disk_cache(str(tmp_path))

    cache_set("abc123", "first")
    cache_set("abc123", "second")

    assert cache_get("abc123") == "second"


def test_different_keys_do_not_collide(tmp_path):
    cache_get, cache_set = make_disk_cache(str(tmp_path))

    cache_set("key-one", "one")
    cache_set("key-two", "two")

    assert cache_get("key-one") == "one"
    assert cache_get("key-two") == "two"


def test_creates_the_cache_directory_on_first_write(tmp_path):
    data_dir = str(tmp_path)
    cache_get, cache_set = make_disk_cache(data_dir)

    cache_set("abc123", "hello")

    assert os.path.isdir(os.path.join(data_dir, "plugin_resolve_cache"))


def test_does_not_create_the_cache_directory_just_by_reading(tmp_path):
    data_dir = str(tmp_path)
    cache_get, _ = make_disk_cache(data_dir)

    cache_get("abc123")

    assert not os.path.exists(os.path.join(data_dir, "plugin_resolve_cache"))


def test_survives_a_fresh_make_disk_cache_call_pointed_at_the_same_data_dir(tmp_path):
    # Simulates a process restart -- the cache is disk-backed, so a brand new make_disk_cache()
    # call (as would happen after a server restart) still sees entries written by a previous one.
    data_dir = str(tmp_path)
    _, cache_set = make_disk_cache(data_dir)
    cache_set("abc123", "still here")

    cache_get_again, _ = make_disk_cache(data_dir)

    assert cache_get_again("abc123") == "still here"


def test_handles_multi_byte_unicode_content(tmp_path):
    cache_get, cache_set = make_disk_cache(str(tmp_path))

    cache_set("abc123", "안녕하세요 \U0001F600")

    assert cache_get("abc123") == "안녕하세요 \U0001F600"
