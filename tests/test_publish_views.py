"""TDD for web.py's Publish REST/render views (publish_upload_view, publish_view) -- no HTTP-level
test existed for either before this (see 19_permalink_지원.md, pumice repo, for why: adding
`permalink` frontmatter support touches both the upload path and the render/routing path, so this
is where regression coverage for the whole Publish feature's HTTP surface starts). Same style as
test_vault_share_views.py: call the view function directly with a minimal fake request exposing
exactly what these views read (.context/.headers/.body/.registry.settings/.matchdict/
.accept_language), rather than standing up a full WSGI app -- Pyramid's own routing/permission
enforcement (unchanged code) isn't re-verified here, only the view bodies' actual behavior against
a real repository and real filesystem (via tmp_path, since these views do direct file I/O)."""

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


class FakeAcceptLanguage:
    def best_match(self, locales, default_match=None):
        return default_match


class FakeRegistry:
    def __init__(self, settings):
        self.settings = settings


class FakeRequest:
    def __init__(self, repository, data_dir, owner=None, vault_id=None,
                 headers=None, body=b"", matchdict=None, scheme="https", host="example.com",
                 json_body=None, query_string="", domain=None, params=None):
        self.repository = repository
        self.context = FakeContext(owner, vault_id) if owner is not None else None
        self.headers = headers or {}
        self.body = body
        self.registry = FakeRegistry({"data_dir": str(data_dir)})
        self.matchdict = matchdict or {}
        self.accept_language = FakeAcceptLanguage()
        self.scheme = scheme
        self.host = host
        self.query_string = query_string
        self.domain = domain if domain is not None else host.split(":")[0]
        self.params = params or {}
        self._json_body = json_body if json_body is not None else {}

    @property
    def json_body(self):
        return self._json_body


def upload(repo, tmp_path, owner, vault_id, path, content: bytes, permalink=None, content_hash="h",
           description=None, image=None, aliases=None):
    headers = {"obs-path": path, "obs-hash": content_hash}
    if permalink is not None:
        headers["obs-permalink"] = permalink
    if description is not None:
        headers["obs-description"] = description
    if image is not None:
        headers["obs-image"] = image
    if aliases is not None:
        headers["obs-aliases"] = json.dumps(aliases)
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


# ─── publish_view: aliases-based old-URL redirect (see 22_aliases_리다이렉트_및_파비콘_자동감지.md) ─

def test_visiting_an_old_url_registered_as_an_alias_serves_the_current_note(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "Tutorials/How to make friends.md", b"# New home",
           aliases=["Guides/Making friends"])

    result = render(repo, tmp_path, "alice", "vault1", "Guides/Making friends")

    assert "New home" in result["content"]


def test_alias_lookup_is_case_insensitive(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "target.md", b"# Target", aliases=["Guides/Old Path"])

    result = render(repo, tmp_path, "alice", "vault1", "guides/old path")

    assert "Target" in result["content"]


def test_alias_is_only_consulted_after_permalink_and_literal_path_both_miss(repo, tmp_path):
    # A literal file genuinely living at this path must win over some other file's alias
    # claiming the same string -- aliases are the last-resort fallback, not a first check.
    upload(repo, tmp_path, "alice", "vault1", "real.md", b"# Real")
    upload(repo, tmp_path, "alice", "vault1", "other.md", b"# Other", aliases=["real.md"])

    result = render(repo, tmp_path, "alice", "vault1", "real.md")

    assert "Real" in result["content"]
    assert "Other" not in result["content"]


def test_republishing_without_the_alias_stops_the_redirect(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "target.md", b"# Target", aliases=["old/path"])
    upload(repo, tmp_path, "alice", "vault1", "target.md", b"# Target")  # re-published, no aliases

    response = render(repo, tmp_path, "alice", "vault1", "old/path")

    assert response.status_code == 404


def test_removing_a_published_file_also_removes_its_aliases(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "target.md", b"# Target", aliases=["old/path"])
    request = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1",
                           json_body={"path": "target.md"})
    result = web.publish_remove_view(request)
    assert result == {"ok": True}

    response = render(repo, tmp_path, "alice", "vault1", "old/path")

    assert response.status_code == 404


def test_unmatched_alias_still_404s(repo, tmp_path):
    response = render(repo, tmp_path, "alice", "vault1", "no/such/alias")

    assert response.status_code == 404


# ─── publish_view: favicon auto-detection (see 22_aliases_리다이렉트_및_파비콘_자동감지.md) ────

def test_favicon_ico_produces_a_plain_icon_link(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "favicon.ico", b"fake-ico-bytes")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["favicons"] == [{"href": "/publish/alice/vault1/favicon.ico", "sizes": None}]


def test_sized_favicon_png_produces_a_sizes_attribute(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "favicon-32x32.png", b"fake-png-bytes")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["favicons"] == [{"href": "/publish/alice/vault1/favicon-32x32.png", "sizes": "32x32"}]


def test_favicon_without_the_x_dimension_still_gets_a_square_sizes_attribute(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "favicon-32.png", b"fake-png-bytes")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["favicons"] == [{"href": "/publish/alice/vault1/favicon-32.png", "sizes": "32x32"}]


def test_multiple_favicon_sizes_all_produce_links(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "favicon-32x32.png", b"a")
    upload(repo, tmp_path, "alice", "vault1", "favicon-192x192.png", b"b")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    sizes = {icon["sizes"] for icon in result["favicons"]}
    assert sizes == {"32x32", "192x192"}


def test_no_favicon_files_means_an_empty_list(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["favicons"] == []


def test_an_unrelated_png_is_not_mistaken_for_a_favicon(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "Attachments/cover.png", b"a")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["favicons"] == []


def test_root_file_list_page_also_gets_favicon_links(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "favicon.ico", b"a")

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["favicons"] == [{"href": "/publish/alice/vault1/favicon.ico", "sizes": None}]


# ─── publish_view: publish.css/publish.js static assets (see 23_사이트_커스터마이징_정적_에셋.md) ─

def test_published_publish_css_is_linked(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "publish.css", b"body{color:red}")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["publish_css_href"] == "/publish/alice/vault1/publish.css"


def test_no_published_publish_css_means_no_link(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["publish_css_href"] is None


def test_a_publish_css_outside_the_vault_root_is_not_linked(repo, tmp_path):
    # Real Obsidian requires publish.css to be in the vault root (see Customize your site.md).
    upload(repo, tmp_path, "alice", "vault1", "Attachments/publish.css", b"body{color:red}")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["publish_css_href"] is None


def test_published_publish_js_is_never_linked_without_a_real_custom_domain(repo, tmp_path):
    # Real Obsidian only loads publish.js on a custom domain (untrusted JS on the shared
    # publish.obsidian.md origin would be a real security hole) -- pumice-server has no genuine
    # per-vault origin isolation yet (publish_customurl_view is a stub), so this must stay
    # inert rather than executing arbitrary uploaded JS on the shared origin.
    upload(repo, tmp_path, "alice", "vault1", "publish.js", b"alert(1)")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["publish_js_href"] is None


def test_root_file_list_page_also_gets_publish_css_link(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "publish.css", b"body{color:red}")

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["publish_css_href"] == "/publish/alice/vault1/publish.css"


# ─── publish_view: first-batch site options (see 25_사이트_옵션_1차_구현.md) ────────────────

def save_options(repo, tmp_path, owner, vault_id, options):
    request = FakeRequest(repo, tmp_path, owner=owner, vault_id=vault_id, json_body={"options": options})
    result = web.publish_options_view(request)
    assert "error" not in result
    return result


def test_site_name_defaults_to_the_vault_id(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["site_name"] == "vault1"


def test_site_name_override_is_used_instead(repo, tmp_path):
    save_options(repo, tmp_path, "alice", "vault1", {"siteName": "My Cool Site"})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["site_name"] == "My Cool Site"


def test_logo_path_resolves_to_a_published_url(repo, tmp_path):
    save_options(repo, tmp_path, "alice", "vault1", {"logo": "Attachments/logo.png"})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["logo_href"] == "https://example.com/publish/alice/vault1/Attachments/logo.png"


def test_no_logo_set_means_no_logo_href(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["logo_href"] is None


def test_noindex_defaults_to_false(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["noindex"] is False


def test_noindex_can_be_enabled(repo, tmp_path):
    save_options(repo, tmp_path, "alice", "vault1", {"noindex": True})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["noindex"] is True


def test_hide_title_defaults_to_false(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["hide_title"] is False


def test_hide_title_can_be_enabled(repo, tmp_path):
    save_options(repo, tmp_path, "alice", "vault1", {"hideTitle": True})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["hide_title"] is True


def test_readable_line_length_defaults_to_true(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["readable_line_length"] is True


def test_readable_line_length_can_be_disabled(repo, tmp_path):
    save_options(repo, tmp_path, "alice", "vault1", {"readableLineLength": False})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["readable_line_length"] is False


def test_strict_line_breaks_off_by_default_collapses_single_newlines(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"line one\nline two")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert "<br" not in result["content"]


def test_strict_line_breaks_enabled_turns_single_newlines_into_br(repo, tmp_path):
    save_options(repo, tmp_path, "alice", "vault1", {"strictLineBreaks": True})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"line one\nline two")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert "<br" in result["content"]


def test_google_analytics_is_stored_but_never_rendered(repo, tmp_path):
    # Real Obsidian only activates analytics on a custom domain (same reasoning as publish.js) --
    # pumice-server has no such isolation yet, so this stays inert (see 25_사이트_옵션_1차_구현.md).
    save_options(repo, tmp_path, "alice", "vault1", {"googleAnalytics": "G-ABC123"})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert "G-ABC123" not in result["content"]
    assert "google_analytics_id" not in result or not result.get("google_analytics_id")


def test_index_file_is_served_at_the_root_when_set_and_published(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "Home.md", b"# Welcome home")
    save_options(repo, tmp_path, "alice", "vault1", {"indexFile": "Home.md"})

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["files"] is None
    assert "Welcome home" in result["content"]


def test_index_file_falls_back_to_the_file_list_when_not_published(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")
    save_options(repo, tmp_path, "alice", "vault1", {"indexFile": "Missing.md"})

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["files"] is not None


def test_no_index_file_set_shows_the_file_list_as_before(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["files"] is not None


# ─── custom domains (see 26_커스텀_도메인_지원.md) ──────────────────────────────────────────────

def test_no_custom_domain_still_hides_publish_js_and_google_analytics(repo, tmp_path):
    # Regression guard for the previously-hardcoded _CUSTOM_DOMAINS_SUPPORTED flag.
    save_options(repo, tmp_path, "alice", "vault1", {"googleAnalytics": "G-ABC123"})
    upload(repo, tmp_path, "alice", "vault1", "publish.js", b"alert(1)")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["publish_js_href"] is None
    assert result["google_analytics"] is None


def test_custom_domain_configured_renders_publish_js_and_google_analytics(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com")
    save_options(repo, tmp_path, "alice", "vault1", {"googleAnalytics": "G-ABC123"})
    upload(repo, tmp_path, "alice", "vault1", "publish.js", b"alert(1)")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["publish_js_href"] == "https://notes.example.com/publish.js"
    assert result["google_analytics"] == "G-ABC123"


def test_this_applies_even_when_visited_via_the_old_publish_path_url(repo, tmp_path):
    # Core design decision: is_custom_domain reflects the vault's own configuration, not which
    # route this particular request came in through (matches real Obsidian's isCustomDomain()).
    repo.set_custom_domain("alice", "vault1", "notes.example.com", redirect=False)
    upload(repo, tmp_path, "alice", "vault1", "publish.js", b"alert(1)")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["publish_js_href"] is not None


def test_hrefs_use_the_custom_domain_root_when_configured(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com")
    upload(repo, tmp_path, "alice", "vault1", "favicon.ico", b"a")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["favicons"] == [{"href": "https://notes.example.com/favicon.ico", "sizes": None}]
    assert result["url_prefix"] == "https://notes.example.com/"


def test_wikilinks_use_the_custom_domain_root_when_configured(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com")
    upload(repo, tmp_path, "alice", "vault1", "target.md", b"# Target")
    upload(repo, tmp_path, "alice", "vault1", "source.md", b"See [[target]] for more.")

    result = render(repo, tmp_path, "alice", "vault1", "source.md")

    assert 'https://notes.example.com/target' in result["content"]


def test_og_image_uses_the_custom_domain_origin_not_the_request_host(repo, tmp_path):
    # Regression test: the request may still arrive via the old /publish/{user}/{vault}/ path
    # (request.host is this server's own host there), but the vault's canonical origin is the
    # custom domain -- og:image must reflect that, not request.host.
    repo.set_custom_domain("alice", "vault1", "notes.example.com")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello", image="Attachments/cover.png")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["image"] == "https://notes.example.com/Attachments/cover.png"


def test_visiting_the_old_url_redirects_when_the_toggle_is_on(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com", redirect=True)
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    response = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert response.status_code == 301
    assert response.location == "https://notes.example.com/note.md"


def test_visiting_the_old_url_does_not_redirect_when_the_toggle_is_off(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com", redirect=False)
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert "Hello" in result["content"]


def test_custom_domain_view_resolves_the_owner_from_the_hostname_and_does_not_redirect(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com", redirect=True)
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    request = FakeRequest(repo, tmp_path, matchdict={"path": "note.md"}, domain="notes.example.com")
    result = web.custom_domain_view(request)

    assert "Hello" in result["content"]


def test_custom_domain_view_404s_for_a_domain_that_was_just_released(repo, tmp_path):
    request = FakeRequest(repo, tmp_path, matchdict={"path": ""}, domain="nowhere.example.com")

    response = web.custom_domain_view(request)

    assert response.status_code == 404


# ─── reverse-proxy custom domain (별도 대형 기능, 35_실제_아키텍처_전환_리버스_프록시_커스텀_도메인.md) ──

def test_parse_custom_domain_url_param_splits_hostname_and_path():
    assert web._parse_custom_domain_url_param("mysite.com/note.md") == ("mysite.com", "note.md")


def test_parse_custom_domain_url_param_with_no_path():
    assert web._parse_custom_domain_url_param("mysite.com") == ("mysite.com", "")


def test_parse_custom_domain_url_param_lowercases_and_strips_whitespace():
    assert web._parse_custom_domain_url_param("  MySite.COM/Note.md  ") == ("mysite.com", "Note.md")


def test_resolve_custom_domain_hostname_prefers_the_x_header_over_request_host(repo, tmp_path):
    request = FakeRequest(repo, tmp_path, domain="proxy-internal.example.com",
                           headers={"x-obsidian-custom-domain": "mysite.com/sub"})

    assert web._resolve_custom_domain_hostname(request) == "mysite.com"


def test_resolve_custom_domain_hostname_falls_back_to_request_domain_without_the_header(repo, tmp_path):
    request = FakeRequest(repo, tmp_path, domain="notes.example.com")

    assert web._resolve_custom_domain_hostname(request) == "notes.example.com"


def test_custom_domain_view_resolves_via_the_x_header_when_the_host_was_rewritten_by_a_proxy(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com", redirect=False)
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    request = FakeRequest(repo, tmp_path, matchdict={"path": "note.md"}, domain="proxy-internal.example.com",
                           headers={"x-obsidian-custom-domain": "notes.example.com"})
    result = web.custom_domain_view(request)

    assert "Hello" in result["content"]


def test_publish_serve_proxy_view_resolves_the_owner_and_note_path_from_the_url_param(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com", redirect=False)
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    request = FakeRequest(repo, tmp_path, params={"url": "notes.example.com/note.md"})
    result = web.publish_serve_proxy_view(request)

    assert "Hello" in result["content"]


def test_publish_serve_proxy_view_root_path_serves_the_file_listing(repo, tmp_path):
    repo.set_custom_domain("alice", "vault1", "notes.example.com", redirect=False)
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Hello")

    request = FakeRequest(repo, tmp_path, params={"url": "notes.example.com"})
    result = web.publish_serve_proxy_view(request)

    assert result["files"] == ["note.md"]


def test_publish_serve_proxy_view_404s_for_an_unregistered_domain(repo, tmp_path):
    request = FakeRequest(repo, tmp_path, params={"url": "nowhere.example.com/note.md"})

    response = web.publish_serve_proxy_view(request)

    assert response.status_code == 404


# ─── metadata cache (28_실제_아키텍처_전환_Phase1_노트본문.md) ──────────────────────────────────

def test_upload_records_a_metadata_cache_entry_for_a_markdown_file(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n\n[[Other]]\n")

    cache = web.load_publish_meta(str(tmp_path), "alice", "vault1", "metadata_cache.json")
    assert cache["note.md"]["headings"] == [
        {"heading": "Title", "level": 1, "pos": [0, 0, 0, 0, 7, 7]}
    ]
    assert [l["link"] for l in cache["note.md"]["links"]] == ["Other"]


def test_upload_records_a_null_entry_for_a_non_markdown_file(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "image.png", b"\x89PNG")

    cache = web.load_publish_meta(str(tmp_path), "alice", "vault1", "metadata_cache.json")
    assert cache["image.png"] is None


def test_reuploading_a_file_recomputes_its_metadata_cache_entry(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# First\n")
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Second\n")

    cache = web.load_publish_meta(str(tmp_path), "alice", "vault1", "metadata_cache.json")
    assert cache["note.md"]["headings"][0]["heading"] == "Second"


def test_removing_a_file_deletes_its_metadata_cache_entry(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")
    request = FakeRequest(repo, tmp_path, owner="alice", vault_id="vault1", json_body={"path": "note.md"})
    result = web.publish_remove_view(request)
    assert result == {"ok": True}

    cache = web.load_publish_meta(str(tmp_path), "alice", "vault1", "metadata_cache.json")
    assert "note.md" not in cache


def test_render_cache_json_reserved_path_returns_the_metadata_cache(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    response = render(repo, tmp_path, "alice", "vault1", "_cache.json")

    assert response.content_type == "application/json"
    assert json.loads(response.body)["note.md"]["headings"][0]["heading"] == "Title"


def test_render_cache_json_merges_in_permalinks_from_the_db(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n", permalink="/pretty/")

    response = render(repo, tmp_path, "alice", "vault1", "_cache.json")

    assert json.loads(response.body)["note.md"]["permalink"] == "/pretty/"


def test_render_raw_reserved_path_returns_raw_markdown(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n\n[[Wikilink]]\n")

    response = render(repo, tmp_path, "alice", "vault1", "_raw/note.md")

    assert response.content_type == "text/markdown"
    assert response.body == b"# Title\n\n[[Wikilink]]\n"


def test_render_raw_reserved_path_404s_for_a_missing_file(repo, tmp_path):
    response = render(repo, tmp_path, "alice", "vault1", "_raw/missing.md")

    assert response.status_code == 404


def test_render_raw_reserved_path_blocks_path_traversal(repo, tmp_path):
    response = render(repo, tmp_path, "alice", "vault1", "_raw/../../etc/passwd")

    assert response.status_code == 404


def test_note_without_enabled_plugins_is_client_rendered(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n\nHello.\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["client_rendered"] is True
    assert result["path"] == "note.md"
    assert json.loads(result["preload_json"]) == {
        "url_prefix": "/publish/alice/vault1/", "path": "note.md", "sliding_window_mode": False,
    }
    # description auto-generation still relies on server-side html_content -- unaffected.
    assert result["description"] == "Title Hello."


def test_note_with_an_enabled_plugin_stays_server_rendered(repo, tmp_path):
    repo.enable_plugin("alice", "vault1", "immich", 1000)
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n\nHello.\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["client_rendered"] is False
    assert result["preload_json"] is None
    assert "Hello" in result["content"]


# ─── search (28_실제_아키텍처_전환_Phase1_노트본문.md's Phase 3 follow-up) ───────────────────────

def test_upload_records_search_text_in_the_metadata_cache(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n\nFind this phrase here.\n")

    cache = web.load_publish_meta(str(tmp_path), "alice", "vault1", "metadata_cache.json")
    assert "Find this phrase here." in cache["note.md"]["searchText"]


def test_show_search_defaults_to_true(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["show_search"] is True


def test_show_search_reflects_a_saved_false_value(repo, tmp_path):
    web.save_publish_meta(str(tmp_path), "alice", "vault1", "options.json", {"showSearch": False})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["show_search"] is False


def test_root_file_listing_page_still_gets_a_preload_script_for_search(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "")

    assert result["files"] is not None
    assert json.loads(result["preload_json"]) == {"url_prefix": "/publish/alice/vault1/", "path": None}


# ─── navigation sidebar + sliding window (28_실제_아키텍처_전환_Phase1_노트본문.md's Phase 5) ────

def test_navigation_tree_groups_nested_files_under_their_top_level_folder(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "Home.md", b"# Home\n")
    upload(repo, tmp_path, "alice", "vault1", "Notes/A.md", b"# A\n")
    upload(repo, tmp_path, "alice", "vault1", "Notes/B.md", b"# B\n")

    result = render(repo, tmp_path, "alice", "vault1", "Home.md")

    tree = {entry["display"]: entry for entry in result["navigation_tree"]}
    assert tree["Home"]["type"] == "file"
    assert tree["Home"]["href"] == "/publish/alice/vault1/Home.md"
    assert tree["Notes"]["type"] == "folder"
    assert sorted(c["display"] for c in tree["Notes"]["children"]) == ["A", "B"]


def test_navigation_tree_respects_ordering_and_hidden_items(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "A.md", b"# A\n")
    upload(repo, tmp_path, "alice", "vault1", "B.md", b"# B\n")
    upload(repo, tmp_path, "alice", "vault1", "C.md", b"# C\n")
    web.save_publish_meta(str(tmp_path), "alice", "vault1", "options.json", {
        "navigationOrdering": ["C.md", "A.md"], "navigationHiddenItems": ["B.md"],
    })

    result = render(repo, tmp_path, "alice", "vault1", "A.md")

    assert [e["display"] for e in result["navigation_tree"]] == ["C", "A"]


def test_show_navigation_and_show_search_default_to_expected_values(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["show_navigation"] is False
    assert result["show_search"] is True


def test_note_preload_json_carries_sliding_window_mode(repo, tmp_path):
    web.save_publish_meta(str(tmp_path), "alice", "vault1", "options.json", {"slidingWindowMode": True})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert json.loads(result["preload_json"])["sliding_window_mode"] is True


# ─── theme switching (별도 대형 기능, 33_실제_아키텍처_전환_테마_전환.md) ────────────────────────

def test_theme_defaults_to_dark_preserving_the_existing_look(repo, tmp_path):
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["theme_attr"] == "dark"
    assert result["show_theme_toggle"] is False


def test_theme_attr_reflects_a_saved_light_default(repo, tmp_path):
    web.save_publish_meta(str(tmp_path), "alice", "vault1", "options.json", {"defaultTheme": "light"})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["theme_attr"] == "light"


def test_theme_attr_is_none_for_system_so_the_page_defers_to_prefers_color_scheme(repo, tmp_path):
    web.save_publish_meta(str(tmp_path), "alice", "vault1", "options.json", {"defaultTheme": "system"})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["theme_attr"] is None


def test_show_theme_toggle_reflects_a_saved_true_value(repo, tmp_path):
    web.save_publish_meta(str(tmp_path), "alice", "vault1", "options.json", {"showThemeToggle": True})
    upload(repo, tmp_path, "alice", "vault1", "note.md", b"# Title\n")

    result = render(repo, tmp_path, "alice", "vault1", "note.md")

    assert result["show_theme_toggle"] is True
