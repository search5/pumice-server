# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- Server-side publish plugins: a discover/enable/run lifecycle for extending
  what a published page can render. A plugin is a normal Python package
  declared as a build-time dependency (no runtime install, no admin upload
  flow) that resolves its own embed syntax on the processed markdown, without
  the core ever needing to know what that syntax is. See
  `docs/PLUGIN_DEVELOPMENT.md` for the full guide.
- Plugin enablement is scoped per vault, not per account — the same way
  Obsidian itself scopes plugins per vault. Managed self-service from the
  vault owner's Publish tab.
- A generic, per-vault `plugin_config` store: a plugin can optionally declare
  a `CONFIG_FIELDS` schema so the Publish tab renders a real labeled settings
  form (text/password fields) instead of a raw JSON textarea, without the
  core knowing what any of the fields mean. Stored encrypted at rest
  (`PLUGIN_CONFIG_ENCRYPTION_KEY`); password-typed values are never sent back
  to the browser once saved.
- Plugin resolve results are cached to disk, keyed by (owner, vault, plugin,
  content, config) — survives restarts, is warmed eagerly right after a note
  is published (so the first real visitor never waits on a plugin's own
  external dependency), and is skipped whenever a plugin's output is
  identical to its input, so a transient failure self-heals on the next
  view/upload instead of being permanently cached as if it were a real
  result.
- Optional plugin autofill: a plugin can declare which of its own
  vault-relative files it wants read (e.g. another plugin's already-synced,
  non-secret settings file) and a function to turn their contents into
  suggested config values, so the vault owner isn't stuck hand-copying data
  that's already sitting in their own vault. Password-typed fields are always
  stripped from suggestions server-side, regardless of what a plugin returns.
- `docs/PLUGIN_DEVELOPMENT.md` (and `.ko.md`): the canonical guide for
  building a server plugin — the `resolve()` contract, config, autofill,
  caching behavior and what it means for how you write a plugin, testing,
  and a pre-ship checklist.
- MCP OAuth 2.1 browser-auth flow, so an MCP client (Claude Code/Desktop)
  can connect with just the server URL — the flow is a thin browser-consent
  wrapper around minting an ordinary, already-existing device token, not a
  parallel credential system.
- Static assets (`dashboard.js`, `login.js`) are now served with a
  cache-busting version query string derived from the file's own mtime, so
  an edit is always picked up on next page load without needing a hard
  refresh.

### Changed

- All HTML moved out of `web.py` into Jinja2 templates.
- Server-side i18n migrated from a custom JSON-based system to Babel/gettext.
  Message ids are unchanged (the existing dotted keys), so no call site had
  to change; the fallback locale is now English instead of Korean.

### Fixed

- A server upgrading past the introduction of vault-scoped plugin enablement
  would crash on startup (and on every subsequent plugin-list request) with
  `no such column: plugin_enablement.vault_id` — the table's schema changed
  but nothing migrated a pre-existing database. Now migrated automatically on
  startup; old (unrecoverable, since the old schema had no vault information
  at all) enablement rows are dropped rather than guessed at.
- The plugin config settings modal was missing the `common.button_save`
  translation key entirely (showing the literal key name instead of
  "Save"/"저장"), and its save button lived inside the scrollable content
  area, requiring a scroll to reach once a plugin had enough fields or
  autofill suggestions to overflow the fixed-height panel. Save now lives in
  the modal's fixed footer next to Close, and the modal's content area sizes
  to the viewport (`70vh`) instead of a fixed `420px`.
