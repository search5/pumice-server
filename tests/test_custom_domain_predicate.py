"""TDD for web.py's custom_domain_route_predicate -- the Pyramid route predicate that decides
whether an incoming request's Host header belongs to a registered custom domain (see
26_커스텀_도메인_지원.md, pumice repo). Deliberately a plain (info, request) function (not a
predicate-factory class) so it can be unit tested here without going through real Pyramid route
matching -- a minimal fake request exposing just .domain/.repository/.headers is enough.

The x-obsidian-custom-domain reverse-proxy header case (see
35_실제_아키텍처_전환_리버스_프록시_커스텀_도메인.md) is covered separately, alongside
custom_domain_view/publish_serve_proxy_view, in test_publish_views.py."""

import pytest

from pumice_server import web
from pumice_server.repository import SqlAlchemyMetadataRepository


@pytest.fixture
def repo():
    r = SqlAlchemyMetadataRepository("sqlite:///:memory:")
    r.init_db()
    return r


class FakePredicateRequest:
    def __init__(self, domain, repository):
        self.domain = domain
        self.repository = repository
        self.headers = {}


def test_predicate_true_when_hostname_is_registered(repo):
    repo.set_custom_domain("alice", "vault1", "notes.example.com")

    assert web.custom_domain_route_predicate({}, FakePredicateRequest("notes.example.com", repo)) is True


def test_predicate_false_for_an_unregistered_host(repo):
    assert web.custom_domain_route_predicate({}, FakePredicateRequest("random.example.com", repo)) is False


def test_predicate_is_case_insensitive(repo):
    repo.set_custom_domain("alice", "vault1", "notes.example.com")

    assert web.custom_domain_route_predicate({}, FakePredicateRequest("NOTES.EXAMPLE.COM", repo)) is True


def test_predicate_false_after_the_domain_is_released(repo):
    repo.set_custom_domain("alice", "vault1", "notes.example.com")
    repo.set_custom_domain("alice", "vault1", "")

    assert web.custom_domain_route_predicate({}, FakePredicateRequest("notes.example.com", repo)) is False
