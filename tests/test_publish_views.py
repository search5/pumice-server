"""TDD for web.py's Publish REST/render views (publish_upload_view, publish_view) -- no HTTP-level
test existed for either before this (see 19_permalink_지원.md, pumice repo, for why: adding
`permalink` frontmatter support touches both the upload path and the render/routing path, so this
is where regression coverage for the whole Publish feature's HTTP surface starts). Same style as
test_vault_share_views.py: call the view function directly with a minimal fake request exposing
exactly what these views read (.context/.headers/.body/.registry.settings/.matchdict/
.accept_language), rather than standing up a full WSGI app -- Pyramid's own routing/permission
enforcement (unchanged code) isn't re-verified here, only the view bodies' actual behavior against
a real repository and real filesystem (via tmp_path, since these views do direct file I/O)."""

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


class FakeAcceptLanguage:
    def best_match(self, locales, default_match=None):
        return default_match


class FakeRegistry:
    def __init__(self, settings):
        self.settings = settings


class FakeRequest:
    def __init__(self, repository, data_dir, owner=None, vault_id=None,
                 headers=None, body=b"", matchdict=None, scheme="https", host="example.com"):
        self.repository = repository
        self.context = FakeContext(owner, vault_id) if owner is not None else None
        self.headers = headers or {}
        self.body = body
        self.registry = FakeRegistry({"data_dir": str(data_dir)})
        self.matchdict = matchdict or {}
        self.accept_language = FakeAcceptLanguage()
        self.scheme = scheme
        self.host = host


def upload(repo, tmp_path, owner, vault_id, path, content: bytes, permalink=None, content_hash="h",
           description=None, image=None):
    headers = {"obs-path": path, "obs-hash": content_hash}
    if permalink is not None:
        headers["obs-permalink"] = permalink
    if description is not None:
        headers["obs-description"] = description
    if image is not None:
        headers["obs-image"] = image
    request = FakeRequest(repo, tmp_path, owner=owner, vault_id=vault_id, headers=headers, body=content)
    result = web.publish_upload_view(request)
    assert result == {"ok": True}


def render(repo, tmp_path, username, vault_id, path):
    request = FakeRequest(repo, tmp_path, matchdict={"username": username, "vault_id": vault_id, "path": path})
    return web.publish_view(request)


# ─── publish_upload_view ────────────────────────────────────────────────────────────────────────

def test_upload_writes_the_file_and_records_it_without_a_permalink(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello", content_hash="hash-1")

    assert repo.get_published_files("alice", "vault1") == [
        {"path": "note.md", "hash": "hash-1", "permalink": None, "description": None, "image": None}
    ]
    assert (tmp_path / "published" / "alice" / "vault1" / "note.md").read_bytes() == b"# Hello"


def test_upload_with_a_permalink_header_stores_it(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello", permalink="p/slug")

    assert repo.get_published_file_by_permalink("alice", "vault1", "p/slug") == {"path": "note.md", "hash": "h"}


def test_upload_rejects_a_request_missing_required_headers(repo, tmp_path):
    request = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", headers={}, body=b"x")

    response = web.publish_upload_view(request)

    assert response.status_code == 400


# ─── publish_view: permalink routing ────────────────────────────────────────────────────────────

def test_permalink_url_renders_the_note(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "notes/a.md", b"# A", permalink="pretty/a")

    result = render(repo, tmp_path, "alice", "vault1", "pretty/a")

    assert result["title"] == "a"
    assert "A" in result["content"]


def test_original_path_url_still_works_alongside_the_permalink(repo, tmp_path):
    # Real Obsidian never redirects or blocks the original path once a permalink is set -- both
    # URLs stay simultaneously valid (see 19_permalink_지원.md).
    upload(repo, tmp_path, "alice", "vault1", "notes/a.md", b"# A", permalink="pretty/a")

    result = render(repo, tmp_path, "alice", "vault1", "notes/a.md")

    assert "A" in result["content"]


def test_a_file_with_no_permalink_is_unaffected_by_another_files_permalink(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "notes/a.md", b"# A", permalink="pretty/a")
    upload(repo, tmp_path, "alice", "vault1", "notes/b.md", b"# B")

    result = render(repo, tmp_path, "alice", "vault1", "notes/b.md")

    assert "B" in result["content"]


def test_root_path_always_shows_the_file_list_even_if_a_permalink_is_empty_string(repo, tmp_path):
    # A permalink can only reach the DB as "" via something other than the normal client (the
    # client never sends obs-permalink for a falsy value) -- but the route must never let an
    # empty-string permalink hijack the root URL regardless of how it got there, matching real
    # Obsidian's own "root path never even checks the permalink map" behavior.
    repo.add_published_file("alice", "vault1", "notes/a.md", "h", permalink="")

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["files"] is not None


def test_unknown_permalink_404s_the_same_as_an_unknown_path(repo, tmp_path):
    response = render(repo, tmp_path, "alice", "vault1", "no/such/permalink")

    assert response.status_code == 404


# ─── publish_view: permalink-aware wikilink rewriting ───────────────────────────────────────────

def test_wikilinks_to_a_permalinked_note_use_its_pretty_url(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "target.md", b"# Target", permalink="pretty/target")
    upload(repo, tmp_path, "alice", "vault1", "source.md", b"See [[target]] for more.")

    result = render(repo, tmp_path, "alice", "vault1", "source.md")

    assert '/publish/alice/vault1/pretty/target' in result["content"]
    assert '/publish/alice/vault1/target' not in result["content"]


def test_wikilinks_to_a_note_without_a_permalink_use_the_literal_path(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "target.md", b"# Target")
    upload(repo, tmp_path, "alice", "vault1", "source.md", b"See [[target]] for more.")

    result = render(repo, tmp_path, "alice", "vault1", "source.md")

    assert '/publish/alice/vault1/target' in result["content"]


# ─── publish_view: description/image social-preview meta tags (see 20_description_image_지원.md) ─

def test_explicit_description_is_used_as_is(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello\n\nSome body text.",
           description="A custom description.")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["description"] == "A custom description."


def test_description_is_auto_generated_from_content_when_not_set(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello\n\nJust a short note.")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert "Just a short note." in result["description"]
    assert "<" not in result["description"]  # HTML tags stripped out


def test_auto_generated_description_is_truncated_for_long_content(repo, tmp_path):
    long_body = "word " * 100  # far past the 200-char limit
    upload(repo, tmp_path, "alice", "vault1", "note.md", f"# Title\n\n{long_body}".encode())

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert len(result["description"]) <= 210
    assert result["description"].endswith("...")


def test_explicit_image_path_resolves_to_an_absolute_published_url(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello", image="Attachments/cover.png")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["image"] == "https://example.com/publish/alice/vault1/Attachments/cover.png"


def test_explicit_external_image_url_is_used_as_is(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello", image="https://cdn.example.com/cover.png")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["image"] == "https://cdn.example.com/cover.png"


def test_no_image_means_no_og_image(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["image"] is None


def test_root_file_list_page_has_no_description_or_image(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello", description="d", image="i.png")

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["description"] is None
    assert result["image"] is None
