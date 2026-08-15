"""TDD for web.py's publish_customurl_view (/api/customurl) -- real Obsidian's apiCustomUrl(url,
redirect) posts {token, id, host, url, redirect} to this endpoint for both fetching (no url) and
saving (see 26_커스텀_도메인_지원.md, pumice repo). Same style as test_publish_options_view.py:
call the view function directly with a minimal fake request."""

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
    def __init__(self, repository, owner, vault_id, json_body=None):
        self.repository = repository
        self.context = FakeContext(owner, vault_id)
        self._json_body = json_body if json_body is not None else {}

    @property
    def json_body(self):
        return self._json_body


def test_fetching_with_nothing_saved_returns_defaults(repo):
    result = web.publish_customurl_view(FakeRequest(repo, "alice", "vault1"))

    assert result == {"url": "", "redirect": False}


def test_saving_then_fetching_reflects_it(repo):
    save_result = web.publish_customurl_view(FakeRequest(
        repo, "alice", "vault1",
        json_body={"host": "localhost:8080", "url": "notes.example.com", "redirect": True},
    ))
    assert save_result == {"url": "notes.example.com", "redirect": True}

    fetch_result = web.publish_customurl_view(FakeRequest(repo, "alice", "vault1"))
    assert fetch_result == {"url": "notes.example.com", "redirect": True}


def test_saving_a_hostname_already_claimed_by_another_vault_returns_409(repo):
    web.publish_customurl_view(FakeRequest(
        repo, "alice", "vault1", json_body={"url": "notes.example.com", "redirect": False}))

    response = web.publish_customurl_view(FakeRequest(
        repo, "bob", "vault1", json_body={"url": "notes.example.com", "redirect": False}))

    assert response.status_code == 409
    # The original claim survives the rejected attempt.
    assert repo.get_vault_by_custom_domain("notes.example.com")["owner_username"] == "alice"


def test_saving_an_empty_url_releases_the_domain(repo):
    web.publish_customurl_view(FakeRequest(
        repo, "alice", "vault1", json_body={"url": "notes.example.com", "redirect": False}))

    result = web.publish_customurl_view(FakeRequest(
        repo, "alice", "vault1", json_body={"url": "", "redirect": False}))

    assert result == {"url": "", "redirect": False}


def test_scoped_by_owner_and_vault(repo):
    web.publish_customurl_view(FakeRequest(
        repo, "alice", "vault1", json_body={"url": "notes.example.com", "redirect": False}))

    assert web.publish_customurl_view(FakeRequest(repo, "bob", "vault1")) == {"url": "", "redirect": False}
    assert web.publish_customurl_view(FakeRequest(repo, "alice", "vault2")) == {"url": "", "redirect": False}
