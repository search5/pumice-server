import os

import pytest

from pumice_server.plugin_autofill import read_vault_files, sanitize_autofill_suggestions


# read_vault_files -- reads a plugin-declared list of vault-relative files (e.g. another
# plugin's own data.json) for autofill suggestions. Best-effort: a missing/unreadable/unsafe
# path just maps to None, since the caller (suggest_plugin_autofill) already treats a missing
# file as "nothing to suggest" rather than an error.

@pytest.fixture
def vault_path(tmp_path):
    plugin_dir = tmp_path / ".obsidian" / "plugins" / "other-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "data.json").write_text('{"profiles": []}', encoding="utf-8")
    return str(tmp_path)


def test_read_vault_files_reads_an_existing_file(vault_path):
    result = read_vault_files(vault_path, [".obsidian/plugins/other-plugin/data.json"])

    assert result == {".obsidian/plugins/other-plugin/data.json": '{"profiles": []}'}


def test_read_vault_files_maps_a_missing_file_to_none(vault_path):
    result = read_vault_files(vault_path, [".obsidian/plugins/other-plugin/missing.json"])

    assert result == {".obsidian/plugins/other-plugin/missing.json": None}


def test_read_vault_files_handles_multiple_paths_independently(vault_path):
    result = read_vault_files(vault_path, [
        ".obsidian/plugins/other-plugin/data.json",
        ".obsidian/plugins/other-plugin/missing.json",
    ])

    assert result == {
        ".obsidian/plugins/other-plugin/data.json": '{"profiles": []}',
        ".obsidian/plugins/other-plugin/missing.json": None,
    }


def test_read_vault_files_blocks_path_traversal(vault_path):
    # A secret file just outside the vault root, to prove a "../"-laden path never escapes it.
    secret_path = os.path.join(os.path.dirname(vault_path), "outside-secret.txt")
    with open(secret_path, "w", encoding="utf-8") as f:
        f.write("top secret")
    try:
        result = read_vault_files(vault_path, ["../outside-secret.txt"])
        assert result == {"../outside-secret.txt": None}
    finally:
        os.remove(secret_path)


def test_read_vault_files_maps_undecodable_content_to_none(vault_path):
    binary_path = os.path.join(vault_path, ".obsidian", "plugins", "other-plugin", "binary.dat")
    with open(binary_path, "wb") as f:
        f.write(b"\xff\xfe\x00\x01")

    result = read_vault_files(vault_path, [".obsidian/plugins/other-plugin/binary.dat"])

    assert result == {".obsidian/plugins/other-plugin/binary.dat": None}


def test_read_vault_files_returns_empty_dict_for_no_paths(vault_path):
    assert read_vault_files(vault_path, []) == {}


# sanitize_autofill_suggestions -- defense in depth: strips password-typed CONFIG_FIELDS keys
# from whatever a plugin's suggest_autofill() returned, regardless of what the plugin actually
# put there, since a plugin has no legitimate way to have derived a real secret from a
# non-secret vault file.

def _config_fields():
    return [
        {"key": "server_url", "label": "Server URL", "type": "text"},
        {"key": "token", "label": "API token", "type": "password"},
    ]


def test_sanitize_autofill_suggestions_strips_password_typed_values():
    suggestions = [{"entry_id": "abc", "label": "My Profile", "values": {"server_url": "https://x.example", "token": "leaked-secret"}}]

    result = sanitize_autofill_suggestions(suggestions, _config_fields())

    assert result == [{"entry_id": "abc", "label": "My Profile", "values": {"server_url": "https://x.example"}}]


def test_sanitize_autofill_suggestions_keeps_non_password_values():
    suggestions = [{"entry_id": "abc", "label": "My Profile", "values": {"server_url": "https://x.example"}}]

    result = sanitize_autofill_suggestions(suggestions, _config_fields())

    assert result[0]["values"] == {"server_url": "https://x.example"}


def test_sanitize_autofill_suggestions_drops_entries_with_no_entry_id():
    suggestions = [{"label": "No id", "values": {}}, {"entry_id": "", "values": {}}]

    assert sanitize_autofill_suggestions(suggestions, _config_fields()) == []


def test_sanitize_autofill_suggestions_skips_non_dict_entries():
    suggestions = ["not-a-dict", 123, None]

    assert sanitize_autofill_suggestions(suggestions, _config_fields()) == []


def test_sanitize_autofill_suggestions_defaults_label_to_entry_id_when_missing():
    suggestions = [{"entry_id": "abc", "values": {}}]

    result = sanitize_autofill_suggestions(suggestions, _config_fields())

    assert result == [{"entry_id": "abc", "label": "abc", "values": {}}]


def test_sanitize_autofill_suggestions_with_no_config_fields_keeps_all_values():
    suggestions = [{"entry_id": "abc", "values": {"server_url": "https://x.example"}}]

    result = sanitize_autofill_suggestions(suggestions, [])

    assert result[0]["values"] == {"server_url": "https://x.example"}
