"""TDD for web.py's vault-sync-sharing REST views (see pumice's 14_vault_sharing_설계.md):
vault_share_invite_view/vault_share_list_view/vault_share_remove_view (owner-only, VaultContext-
scoped) and vault_share_leave_view/vault_shared_with_me_view (act on the caller's own account,
not VaultContext).

No web.py view test file existed before this (the coverage drive deliberately left REST views
out of scope -- see llm-wiki/09-test-coverage-drive.md). These call the view functions directly
with a minimal fake request exposing exactly what each view reads (.context/.identity/
.repository/.json_body) rather than standing up a full WSGI app -- Pyramid's own permission/ACL
enforcement (permission='vault-access' via VaultContext.__acl__, unchanged code) isn't
re-verified here, only the new view bodies' actual behavior against a real repository.
"""

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
    def __init__(self, repository, owner=None, vault_id=None, identity=None, json_body=None):
        self.repository = repository
        self.context = FakeContext(owner, vault_id) if owner is not None else None
        self.identity = identity
        self._json_body = json_body if json_body is not None else {}

    @property
    def json_body(self):
        return self._json_body


# ─── vault_share_invite_view (owner-only) ──────────────────────────────────────────────────────

def test_invite_creates_a_pending_share(repo):
    request = FakeRequest(repo, owner="alice", vault_id="vault1", json_body={"email": "Friend@Example.com"})

    result = web.vault_share_invite_view(request)

    assert result["ok"] is True
    assert "uid" in result
    shares = repo.list_vault_shares("alice", "vault1")
    assert shares[0]["email"] == "friend@example.com"
    assert shares[0]["uid"] == result["uid"]


def test_invite_rejects_a_missing_email(repo):
    request = FakeRequest(repo, owner="alice", vault_id="vault1", json_body={})

    response = web.vault_share_invite_view(request)

    assert response.status_code == 400


# ─── vault_share_list_view (owner-only) ────────────────────────────────────────────────────────

def test_list_returns_shares_for_the_context_owner_and_vault(repo):
    repo.add_vault_share("alice", "vault1", "friend@example.com")
    repo.add_vault_share("alice", "otherVault", "someone@example.com")
    request = FakeRequest(repo, owner="alice", vault_id="vault1")

    result = web.vault_share_list_view(request)

    assert len(result["shares"]) == 1
    share = result["shares"][0]
    assert share["email"] == "friend@example.com"
    assert share["accepted"] is False
    assert "createdAtMs" in share


def test_list_is_empty_for_a_vault_with_no_shares(repo):
    request = FakeRequest(repo, owner="alice", vault_id="vault1")

    assert web.vault_share_list_view(request) == {"shares": []}


# ─── vault_share_remove_view (owner-only) ──────────────────────────────────────────────────────

def test_remove_deletes_the_share(repo):
    uid = repo.add_vault_share("alice", "vault1", "friend@example.com")
    request = FakeRequest(repo, owner="alice", vault_id="vault1", json_body={"share_uid": uid})

    result = web.vault_share_remove_view(request)

    assert result == {"ok": True}
    assert repo.list_vault_shares("alice", "vault1") == []


def test_remove_returns_ok_false_for_an_unknown_uid(repo):
    request = FakeRequest(repo, owner="alice", vault_id="vault1", json_body={"share_uid": "nope"})

    assert web.vault_share_remove_view(request) == {"ok": False}


def test_remove_rejects_a_missing_share_uid(repo):
    request = FakeRequest(repo, owner="alice", vault_id="vault1", json_body={})

    response = web.vault_share_remove_view(request)

    assert response.status_code == 400


def test_remove_cannot_reach_a_share_under_a_different_vault_context(repo):
    # VaultContext.owner/vault_id always come from the caller's own authenticated identity plus
    # the request's own vault_id -- a caller acting on "otherVault" can never touch a share row
    # that belongs to "vault1", even with the exact right uid.
    uid = repo.add_vault_share("alice", "vault1", "friend@example.com")
    request = FakeRequest(repo, owner="alice", vault_id="otherVault", json_body={"share_uid": uid})

    assert web.vault_share_remove_view(request) == {"ok": False}
    assert len(repo.list_vault_shares("alice", "vault1")) == 1


# ─── vault_share_leave_view (any authenticated caller, not the owner) ─────────────────────────

def test_leave_removes_the_callers_own_share(repo):
    repo.create_user("bob", "hash", email="bob@example.com")
    repo.add_vault_share("alice", "vault1", "bob@example.com")
    request = FakeRequest(
        repo, identity={"username": "bob"},
        json_body={"ownerUsername": "alice", "vaultId": "vault1"},
    )

    result = web.vault_share_leave_view(request)

    assert result == {"ok": True}
    assert repo.list_vault_shares("alice", "vault1") == []


def test_leave_rejects_missing_owner_or_vault_id(repo):
    request = FakeRequest(repo, identity={"username": "bob"}, json_body={"ownerUsername": "alice"})

    response = web.vault_share_leave_view(request)

    assert response.status_code == 400


# ─── vault_shared_with_me_view ─────────────────────────────────────────────────────────────────

def test_shared_with_me_lists_vaults_shared_to_the_callers_email(repo):
    repo.create_user("bob", "hash", email="bob@example.com")
    repo.add_vault_share("alice", "vault1", "bob@example.com")
    repo.add_vault_share("carol", "vault2", "bob@example.com")
    request = FakeRequest(repo, identity={"username": "bob"})

    result = web.vault_shared_with_me_view(request)

    owners_and_vaults = {(v["ownerUsername"], v["vaultId"]) for v in result["vaults"]}
    assert owners_and_vaults == {("alice", "vault1"), ("carol", "vault2")}


def test_shared_with_me_is_empty_for_an_account_with_no_shares(repo):
    repo.create_user("bob", "hash", email="bob@example.com")
    request = FakeRequest(repo, identity={"username": "bob"})

    assert web.vault_shared_with_me_view(request) == {"vaults": []}
