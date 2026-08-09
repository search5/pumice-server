# Server plugin development guide

A pumice-server plugin extends what a **published page** can render. It runs entirely
server-side, inside the same Python process as the web server, and only ever touches content
that's already been published (`data_dir/published/...`) — it has nothing to do with vault sync,
version history, or the MCP tools. If you haven't read the "Server plugins" section of
[README.md](../README.md) yet, start there for the high-level lifecycle; this doc is the deep
dive for actually building one.

`pumice-plugin-immich` (a sibling package at `../pumice-plugin-immich` relative to this repo, if
you have it checked out) is a complete, real, working example. Concrete references to it appear
throughout this guide — reading its source alongside this doc is the fastest way to see all of
this land in actual code.

## 1. The mental model

- **The core knows nothing about your syntax.** pumice-server has no idea what `![immich:...]`
  means, and never will — that pattern is defined and matched entirely inside
  pumice-plugin-immich's own code. Your plugin is handed the *whole* page's content and is
  responsible for finding and replacing its own syntax within it. This is deliberate: it's the
  only way plugins stay decoupled from the core and from each other.
- **A plugin is a pure `content -> content` function.** No filesystem access, no database access,
  no network access to pumice-server's own internals. If your plugin needs credentials or other
  settings, they arrive via a parameter (see [Config](#4-config-optional)) — you never reach into
  pumice-server's storage yourself.
- **Nothing is installed at runtime.** There's no admin upload flow, no plugin marketplace. A
  plugin is a normal Python package declared as a `pyproject.toml` dependency and baked into the
  Docker image at build time — exactly as consequential as adding any other server dependency,
  and going through the same review/build/redeploy path.

## 2. Minimal skeleton

```
my-plugin/
  pyproject.toml
  my_plugin/
    __init__.py
  tests/
    test_resolve.py
```

```toml
# pyproject.toml
[project]
name = "my-plugin"
version = "0.1.0"
description = "Resolves my-plugin's embed syntax on published pages."
requires-python = ">=3.13"
dependencies = []

[project.entry-points."pumice_server.plugins"]
my-plugin = "my_plugin:resolve"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

```python
# my_plugin/__init__.py
def resolve(content: str, *, owner_username: str = None, vault_id: str = None,
            config_entries: dict = None, **_kwargs) -> str:
    return content  # replace with your own transform
```

That's the entire contract. Everything below is detail on top of this.

## 3. The `resolve()` contract

```python
def resolve(content: str, *, owner_username: str, vault_id: str, config_entries: dict) -> str:
    ...
```

- **`content`** — the note's processed markdown at the point right after wikilinks have been
  resolved (`[[Note]]` → real `<a>`/`<img>` tags) and frontmatter stripped, and right before the
  markdown-to-HTML pass. Returning literal HTML works the same way wikilink resolution's own
  output does — you don't need to produce markdown, HTML is fine.
- **`owner_username` / `vault_id`** — whose vault this is. Almost always just used to look up the
  right entry in `config_entries`; don't use these to reach into any storage of your own; you
  don't have any.
- **`config_entries`** — `{entry_id: {key: value, ...}}`, already decrypted and ready to use.
  Empty dict if the plugin declares no `CONFIG_FIELDS`, or the vault owner hasn't configured any
  entries yet. See [Config](#4-config-optional).
- **Return value** — the (possibly) transformed content. Return `content` unchanged for anything
  you don't recognize or can't currently resolve — never mutate or drop parts of the page you
  don't understand.
- **Accept `**kwargs`.** The signature above is the whole contract today, but always accept
  `**kwargs` (or give every keyword a default) so a future core version can pass an additional
  keyword without breaking your plugin.

### When it's called

`resolve()` runs in two places, both driven by `plugin_loader.run_enabled_plugins()`:

1. **On upload** (`POST /api/upload`) — eagerly, best-effort, right after a note is published, so
   the very first real visitor doesn't have to wait on (or risk) your plugin's own external
   dependency. A failure here is logged and silently ignored; the upload itself always succeeds
   regardless of what your plugin does.
2. **On view** (`GET /publish/...`) — lazily, only on a cache miss (see
   [Caching](#5-caching-and-what-it-means-for-you)).

Both call sites pass identical arguments for the same content — write `resolve()` as a pure
function of its inputs and you don't need to think about which call site you're in.

### Error handling

Never let an unexpected exception escape uncaught if you can help it, but if one does, it's fine
— pumice-server catches it, logs it, and skips your plugin for that render (the page still
renders; your syntax is simply left as-is). A visitor hitting a public page must never see a 500
because your plugin has a bug or an upstream service it depends on is down.

The recommended pattern (see `pumice_plugin_immich._resolve_one`): catch failures yourself, per
embed, and fall back to leaving that one embed's original syntax untouched. This degrades one
broken image without taking the rest of the page down with it, and gives you a chance to log
something more specific than pumice-server's generic "plugin raised" message.

## 4. Config (optional)

If your plugin needs credentials or settings from the vault owner, declare a second, optional
entry point pointing at a plain list describing the fields to collect:

```toml
[project.entry-points."pumice_server.plugin_config_fields"]
my-plugin = "my_plugin:CONFIG_FIELDS"
```

```python
CONFIG_FIELDS = [
    {"key": "server_url", "label": "My service URL", "type": "text"},
    {"key": "token", "label": "API token", "type": "password"},
]
```

This is what lets the vault owner's **Publish** tab render a real, labeled settings form —
never a raw JSON textarea — while pumice-server's own code never learns what any of your keys
mean. It just renders whatever schema you declare.

- **`type`** is `"text"` or `"password"`. A `"password"` field is masked in the form, and — this
  matters — **its stored value is never sent back to the browser** once saved; the form always
  shows it blank. Correspondingly, when the vault owner *updates* an existing entry and leaves a
  password field blank, that means "keep the existing value", not "clear it" — this merge logic
  lives in pumice-server, you don't need to implement anything for it.
- **Entries are per `(owner_username, vault_id, plugin_id, entry_id)`**, letting one plugin
  support multiple named configurations (e.g. pumice-plugin-immich: one entry per Immich
  profile). `entry_id` is a free-text name the vault owner types in when adding an entry — if
  your embed syntax references something like a "profile id", document that the vault owner
  should make the `entry_id` match it (harang-immich's `![immich:profileId/assetId]` and its
  config entries are correlated this way, entirely by convention, not by any enforcement in
  pumice-server).
- **Encrypted at rest** with a server-held key (`PLUGIN_CONFIG_ENCRYPTION_KEY`), decrypted before
  your `resolve()` ever sees it — you never touch ciphertext.
- **Known limitation:** `label` strings are rendered as plain, unlocalized text today — there's
  no i18n hook for plugin-declared config field labels yet. Keep labels short and
  language-neutral (or just clear English) until this exists.

If you don't declare `CONFIG_FIELDS`, `config_entries` is always `{}` and there's no settings UI
for your plugin at all — fine for a plugin that needs no configuration.

## 5. Caching and what it means for you

A published page has no server-side HTML cache of its own — every view re-parses the note from
scratch. What *is* cached is each plugin's `resolve()` output, keyed by
`(owner, vault, plugin_id, content, config)` — see `plugin_loader.py` and
`plugin_resolve_disk_cache.py`. This is transparent to you (you never see cache_get/cache_set),
but one behavior directly affects how you should write your plugin:

> **`resolve()`'s output is only cached when it's different from its input.**

If your plugin returns `content` completely unchanged — whether because nothing in it matched
your syntax, or because you caught an internal failure and fell back to leaving everything as-is
— that call is **never cached**, and will be retried live on every future view/upload until it
actually produces a different result. This is deliberate self-healing: a transient failure (your
external dependency being briefly down) never gets permanently "baked in" the way a real result
would.

Practical implications:

- A **partial** success (some embeds resolved, one broken one left as literal syntax) *is*
  cached as a whole. If the broken part is broken because the target genuinely doesn't exist
  (e.g. a deleted asset), it'll show as broken until the note or config changes — which is
  correct. If it's broken because of a transient outage, the whole page re-resolves next time
  (nothing was cached), so the broken part gets another chance too.
- Don't do your own long-lived caching *across* calls for something the pumice-server cache
  already handles (the resolved output itself). Do keep whatever's cheap and process-local
  though — e.g. an auth token obtained once and reused for multiple embeds *within* a single
  `resolve()` call, since that's about avoiding redundant work inside one call, not something the
  outer cache is responsible for.
- Keep external calls reasonably bounded (a short timeout) — a slow `resolve()` blocks either an
  upload response or a page render.

## 6. Testing your plugin

TDD is the expected workflow in this codebase (see the main repo's own test suite for the style),
and it applies just as much to plugins:

- Mock your external HTTP dependency (the [`responses`](https://github.com/getsentry/responses)
  library is what pumice-plugin-immich's test suite uses) rather than hitting a real service.
- Cover the graceful-degradation paths explicitly: no config for a given id, incomplete config
  (missing a required key), the external call failing outright, and one broken embed not blocking
  others on the same page.
- If you're mirroring an existing Obsidian client plugin's embed syntax, add a regex-parity test
  against the client plugin's own pattern so the two never silently drift apart.
- Escape anything you interpolate into HTML output (alt text, labels, anything else that could
  contain user-authored text) — test this explicitly. A plugin that emits raw HTML is a real XSS
  surface if it doesn't.

For a full round-trip check beyond your plugin's own unit tests (i.e. does pumice-server actually
call it, cache it, and render it correctly end-to-end), run a scratch pumice-server instance
locally with your package added as an editable dependency (`uv add --editable ../my-plugin`) and
exercise the real HTTP flow: enable the plugin for a vault, save a config entry, publish a note,
fetch the rendered page.

## 7. Installing into pumice-server

For local development, add it as an editable path dependency:

```toml
# pumice-server's pyproject.toml
[tool.uv.sources]
my-plugin = { path = "../my-plugin", editable = true }
```

```
uv add my-plugin
```

For production, point `[tool.uv.sources]` at wherever the package actually lives (a git source,
a private index, etc.) instead of a local path, and rebuild the Docker image. `discover_plugins()`
(`src/pumice_server/plugin_registry.py`) finds it via
`importlib.metadata.entry_points(group="pumice_server.plugins")` — the same mechanism pytest and
Flask use for their own plugins. There's nothing to configure beyond the entry point declarations
above.

## 8. Enabling and using it

Enabling is self-service and scoped **per vault**, not per account — from the vault owner's
**Publish** tab, for whichever vault is selected, not from My Page. This mirrors how Obsidian
itself scopes plugins per vault: a vault that actually uses your syntax can have your plugin
turned on without it also running (and scanning for syntax that'll never appear) over every other
vault the same account happens to own.

If your plugin declared `CONFIG_FIELDS`, the vault owner also adds one or more named config
entries from the same tab before your plugin can do anything useful — see
[Config](#4-config-optional).

## 9. Checklist before shipping

- [ ] `resolve()` never raises for an expected failure mode (missing config, unreachable
      service, malformed embed) — it catches and degrades gracefully instead.
- [ ] `resolve()` accepts `**kwargs` (or defaults every keyword) for forward compatibility.
- [ ] Anything you interpolate into output HTML is escaped.
- [ ] A `plugin_id` (the entry point name) chosen deliberately — it's the primary key for
      enablement and config storage, so renaming it later means every vault that enabled/
      configured the old name has to redo it under the new one.
- [ ] Unit tests cover: happy path, no config, incomplete config, external failure, one broken
      embed not blocking another on the same page.
- [ ] If declaring `CONFIG_FIELDS`, password-typed keys are actually the secrets (not accidentally
      the other way around) — anything marked `"password"` is masked and never echoed back.
- [ ] Verified end-to-end against a real scratch pumice-server instance, not just your plugin's
      own unit tests.
