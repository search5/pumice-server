"""Coverage for vault sharing's repository layer (see pumice's 14_vault_sharing_설계.md):
the new vault_shares table's CRUD methods, and resolve_vault_owner() -- the single checkpoint
every other sync-auth call site (service.py's _verify_vault_access, ws_sync_resource.py's
_handle_init) will delegate to.

Same style as test_repository_coverage.py: plain functions, a fresh in-memory
SqlAlchemyMetadataRepository per test.
"""

import pytest

from pumice_server.repository import SqlAlchemyMetadataRepository, VaultAccessDenied


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


def make_user(repo, username, email=None):
    repo.create_user(username, "hash", email=email)


# ─── add_vault_share / list_vault_shares ───────────────────────────────────────────────────────

def test_add_vault_share_creates_a_pending_share(repo):
    uid = repo.add_vault_share("owner", "MyVault", "Friend@Example.com")

    shares = repo.list_vault_shares("owner", "MyVault")
    assert len(shares) == 1
    assert shares[0]["uid"] == uid
    assert shares[0]["email"] == "friend@example.com"  # stored lowercased
    assert shares[0]["accepted"] is False
    assert shares[0]["accepted_at_ms"] is None


def test_add_vault_share_is_idempotent_for_the_same_owner_vault_email(repo):
    uid1 = repo.add_vault_share("owner", "MyVault", "friend@example.com")
    uid2 = repo.add_vault_share("owner", "MyVault", "FRIEND@example.com")  # different casing

    assert uid1 == uid2
    assert len(repo.list_vault_shares("owner", "MyVault")) == 1


def test_add_vault_share_scoped_by_owner_and_vault(repo):
    repo.add_vault_share("owner", "MyVault", "friend@example.com")
    repo.add_vault_share("owner", "OtherVault", "friend@example.com")
    repo.add_vault_share("otherOwner", "MyVault", "friend@example.com")

    assert len(repo.list_vault_shares("owner", "MyVault")) == 1
    assert len(repo.list_vault_shares("owner", "OtherVault")) == 1
    assert len(repo.list_vault_shares("otherOwner", "MyVault")) == 1


def test_list_vault_shares_empty_when_none_invited(repo):
    assert repo.list_vault_shares("owner", "MyVault") == []


# ─── remove_vault_share (owner-side revoke) ────────────────────────────────────────────────────

def test_remove_vault_share_deletes_the_row(repo):
    uid = repo.add_vault_share("owner", "MyVault", "friend@example.com")

    assert repo.remove_vault_share("owner", "MyVault", uid) is True
    assert repo.list_vault_shares("owner", "MyVault") == []


def test_remove_vault_share_returns_false_for_unknown_uid(repo):
    assert repo.remove_vault_share("owner", "MyVault", "no-such-uid") is False


def test_remove_vault_share_cannot_cross_owner_or_vault_boundary(repo):
    uid = repo.add_vault_share("owner", "MyVault", "friend@example.com")

    # Guessing the right uid isn't enough -- owner_username and vault_id must match too.
    assert repo.remove_vault_share("attacker", "MyVault", uid) is False
    assert repo.remove_vault_share("owner", "OtherVault", uid) is False
    assert len(repo.list_vault_shares("owner", "MyVault")) == 1


# ─── remove_vault_share_by_invited_user ("leave") ──────────────────────────────────────────────

def test_leave_removes_the_share_matching_the_callers_current_email(repo):
    make_user(repo, "collaborator", email="friend@example.com")
    repo.add_vault_share("owner", "MyVault", "friend@example.com")

    assert repo.remove_vault_share_by_invited_user("collaborator", "owner", "MyVault") is True
    assert repo.list_vault_shares("owner", "MyVault") == []


def test_leave_is_a_noop_when_caller_has_no_email(repo):
    make_user(repo, "collaborator", email=None)
    repo.add_vault_share("owner", "MyVault", "friend@example.com")

    assert repo.remove_vault_share_by_invited_user("collaborator", "owner", "MyVault") is False
    assert len(repo.list_vault_shares("owner", "MyVault")) == 1


def test_leave_is_a_noop_when_no_share_matches(repo):
    make_user(repo, "collaborator", email="someone-else@example.com")
    repo.add_vault_share("owner", "MyVault", "friend@example.com")

    assert repo.remove_vault_share_by_invited_user("collaborator", "owner", "MyVault") is False


# ─── list_shared_vaults_for_user ───────────────────────────────────────────────────────────────

def test_list_shared_vaults_for_user_matches_by_email(repo):
    make_user(repo, "collaborator", email="friend@example.com")
    repo.add_vault_share("owner", "MyVault", "friend@example.com")
    repo.add_vault_share("otherOwner", "TheirVault", "friend@example.com")
    repo.add_vault_share("owner", "UnrelatedVault", "someone-else@example.com")

    shared = repo.list_shared_vaults_for_user("collaborator")

    assert {(s["owner_username"], s["vault_id"]) for s in shared} == {
        ("owner", "MyVault"), ("otherOwner", "TheirVault"),
    }


def test_list_shared_vaults_for_user_empty_when_no_email(repo):
    make_user(repo, "collaborator", email=None)
    assert repo.list_shared_vaults_for_user("collaborator") == []


def test_list_shared_vaults_for_user_empty_when_unknown_user(repo):
    assert repo.list_shared_vaults_for_user("nobody") == []


# ─── get_accepted_share_usernames ──────────────────────────────────────────────────────────────

def test_get_accepted_share_usernames_only_includes_accepted_and_matched_accounts(repo):
    make_user(repo, "accepted-collaborator", email="accepted@example.com")
    make_user(repo, "pending-collaborator", email="pending@example.com")
    repo.add_vault_share("owner", "MyVault", "accepted@example.com")
    repo.add_vault_share("owner", "MyVault", "pending@example.com")
    repo.add_vault_share("owner", "MyVault", "no-account@example.com")

    # Accept only the first share, the way resolve_vault_owner(mark_accepted=True) would.
    repo.resolve_vault_owner("accepted-collaborator", "owner", "MyVault", mark_accepted=True)

    assert repo.get_accepted_share_usernames("owner", "MyVault") == ["accepted-collaborator"]


def test_get_accepted_share_usernames_empty_when_nothing_accepted(repo):
    repo.add_vault_share("owner", "MyVault", "friend@example.com")
    assert repo.get_accepted_share_usernames("owner", "MyVault") == []


# ─── resolve_vault_owner ────────────────────────────────────────────────────────────────────────

def test_resolve_vault_owner_self_owned_short_circuit_no_email_needed(repo):
    # No user row, no email, no vault_shares row at all -- must still succeed for the
    # self-owned case, exactly like today's behavior.
    assert repo.resolve_vault_owner("owner", "owner", "MyVault") == "owner"
    assert repo.resolve_vault_owner("owner", "", "MyVault") == "owner"


def test_resolve_vault_owner_grants_access_for_an_accepted_share(repo):
    make_user(repo, "collaborator", email="friend@example.com")
    repo.add_vault_share("owner", "MyVault", "friend@example.com")
    repo.resolve_vault_owner("collaborator", "owner", "MyVault", mark_accepted=True)

    assert repo.resolve_vault_owner("collaborator", "owner", "MyVault") == "owner"


def test_resolve_vault_owner_grants_access_for_a_still_pending_share(repo):
    # Binary access, no role gate: matches real Obsidian Sync where "Invite Pending" is a status
    # label, not a block on access.
    make_user(repo, "collaborator", email="friend@example.com")
    repo.add_vault_share("owner", "MyVault", "friend@example.com")

    assert repo.resolve_vault_owner("collaborator", "owner", "MyVault") == "owner"
    # And a pending share is untouched unless mark_accepted=True was actually passed.
    assert repo.list_vault_shares("owner", "MyVault")[0]["accepted"] is False


def test_resolve_vault_owner_mark_accepted_flips_the_share_exactly_once(repo):
    make_user(repo, "collaborator", email="friend@example.com")
    repo.add_vault_share("owner", "MyVault", "friend@example.com")

    repo.resolve_vault_owner("collaborator", "owner", "MyVault", mark_accepted=True)
    share = repo.list_vault_shares("owner", "MyVault")[0]
    assert share["accepted"] is True
    first_accepted_at = share["accepted_at_ms"]
    assert first_accepted_at is not None

    # Calling it again must not clobber accepted_at_ms with a new timestamp.
    repo.resolve_vault_owner("collaborator", "owner", "MyVault", mark_accepted=True)
    assert repo.list_vault_shares("owner", "MyVault")[0]["accepted_at_ms"] == first_accepted_at


def test_resolve_vault_owner_denies_when_no_share_exists(repo):
    make_user(repo, "stranger", email="stranger@example.com")

    with pytest.raises(VaultAccessDenied):
        repo.resolve_vault_owner("stranger", "owner", "MyVault")


def test_resolve_vault_owner_denies_when_caller_has_no_email(repo):
    make_user(repo, "collaborator", email=None)
    repo.add_vault_share("owner", "MyVault", "friend@example.com")

    with pytest.raises(VaultAccessDenied):
        repo.resolve_vault_owner("collaborator", "owner", "MyVault")


def test_resolve_vault_owner_denies_when_caller_account_does_not_exist(repo):
    with pytest.raises(VaultAccessDenied):
        repo.resolve_vault_owner("ghost", "owner", "MyVault")


def test_resolve_vault_owner_is_scoped_to_the_exact_vault_shared(repo):
    make_user(repo, "collaborator", email="friend@example.com")
    repo.add_vault_share("owner", "VaultA", "friend@example.com")

    with pytest.raises(VaultAccessDenied):
        repo.resolve_vault_owner("collaborator", "owner", "VaultB")


def test_resolve_vault_owner_denies_immediately_after_owner_revokes(repo):
    make_user(repo, "collaborator", email="friend@example.com")
    uid = repo.add_vault_share("owner", "MyVault", "friend@example.com")
    repo.resolve_vault_owner("collaborator", "owner", "MyVault", mark_accepted=True)

    repo.remove_vault_share("owner", "MyVault", uid)

    with pytest.raises(VaultAccessDenied):
        repo.resolve_vault_owner("collaborator", "owner", "MyVault")
