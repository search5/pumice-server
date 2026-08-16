"""TDD for web.py's publish_options_view (/api/options) -- real Obsidian Publish's "Change site
options" dialog has ~20 settings; this covers the first batch pumice implements (see
24_사이트_옵션_다이얼로그_실제_옵시디언과_비교.md / 25_사이트_옵션_1차_구현.md, pumice repo):
siteName/indexFile/logo/noindex/hideTitle/readableLineLength/strictLineBreaks/googleAnalytics.
Stored in publish_meta/{owner}/{vault}/options.json via the existing load_publish_meta/
save_publish_meta helpers (same mechanism site.json already uses for slug/host) -- no new DB
table. Same style as test_vault_share_views.py: call the view function directly with a minimal
fake request."""

import json

import pytest

from pumice_server import web
from pumice_server.repository import SqlAlchemyMetadataRepository


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class FakeContext:
    def __init__(self, owner, vault_id):
        self.owner = owner
        self.vault_id = vault_id


class FakeRequest:
    def __init__(self, repository, data_dir, owner, vault_id, json_body=None):
        self.repository = repository
        self.context = FakeContext(owner, vault_id)
        self.registry = type("R", (), {"settings": {"data_dir": str(data_dir)}})()
        self._json_body = json_body if json_body is not None else {}

    @property
    def json_body(self):
        return self._json_body


DEFAULTS = {
    "siteName": "", "indexFile": "", "logo": "", "noindex": False, "hideTitle": False,
    "readableLineLength": True, "strictLineBreaks": False, "googleAnalytics": "",
    "showSearch": True,
}


def test_fetching_options_with_nothing_saved_returns_real_obsidians_own_defaults(repo, tmp_path):
    request = FakeRequest(repo, tmp_path, "alice", "vault1", json_body={})

    result = web.publish_options_view(request)

    assert result == {"options": DEFAULTS}


def test_saving_options_then_fetching_reflects_them(repo, tmp_path):
    save_request = FakeRequest(repo, tmp_path, "alice", "vault1",
                                json_body={"options": {"siteName": "My Site", "noindex": True}})
    web.publish_options_view(save_request)

    fetch_request = FakeRequest(repo, tmp_path, "alice", "vault1", json_body={})
    result = web.publish_options_view(fetch_request)

    assert result["options"]["siteName"] == "My Site"
    assert result["options"]["noindex"] is True
    # Untouched keys keep their defaults.
    assert result["options"]["logo"] == ""


def test_saving_again_only_changes_the_given_keys(repo, tmp_path):
    web.publish_options_view(FakeRequest(repo, tmp_path, "alice", "vault1",
                                          json_body={"options": {"siteName": "My Site"}}))
    web.publish_options_view(FakeRequest(repo, tmp_path, "alice", "vault1",
                                          json_body={"options": {"noindex": True}}))

    result = web.publish_options_view(FakeRequest(repo, tmp_path, "alice", "vault1", json_body={}))

    assert result["options"]["siteName"] == "My Site"
    assert result["options"]["noindex"] is True


def test_unknown_keys_in_the_payload_are_ignored(repo, tmp_path):
    web.publish_options_view(FakeRequest(repo, tmp_path, "alice", "vault1",
                                          json_body={"options": {"notARealSetting": "x"}}))

    result = web.publish_options_view(FakeRequest(repo, tmp_path, "alice", "vault1", json_body={}))

    assert "notARealSetting" not in result["options"]


def test_options_are_scoped_by_owner_and_vault(repo, tmp_path):
    web.publish_options_view(FakeRequest(repo, tmp_path, "alice", "vault1",
                                          json_body={"options": {"siteName": "Alice's site"}}))

    bob_result = web.publish_options_view(FakeRequest(repo, tmp_path, "bob", "vault1", json_body={}))
    other_vault_result = web.publish_options_view(FakeRequest(repo, tmp_path, "alice", "vault2", json_body={}))

    assert bob_result["options"]["siteName"] == ""
    assert other_vault_result["options"]["siteName"] == ""
