# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [0.2.11] - 2026-11-06

### Changed

- `bookmarks.json` and `community-plugins.json` are now merged on the server
  (3-way, using the `baseHash` the client sends with `push_req`) instead of the
  later upload overwriting the earlier one. The merged version is broadcast to
  every connected device, including the uploader. Deletions are preserved
  (merge bases are kept for 180 days); a device without a known base merges as
  a union. Clients that do not send `baseHash` keep the old overwrite behavior.

## [0.2.10] - 2026-10-07

### Fixed

- Queries that compared boolean columns with `IS 0` / `IS 1` (`is_(False)`,
  `is_(True)`) failed on CUBRID with a syntax error, which made every upload
  that needed a content-hash lookup fail. They now use `== false()` /
  `== true()`.

## [0.2.9] - 2026-08-15

### Added

- A single persistent WebSocket sync connection (`/ws`) modeled on Obsidian
  core Sync's own protocol (reverse-engineered from the shipped app for
  design/behavior reference only, never copied) replaces gRPC-Web, SSE, and
  protobuf entirely. JSON text frames for control messages, raw binary
  frames for file content.
- Upload/download/history-version-download binary streaming over WS, with an
  explicit `stream_end` signal so the client always knows when a streaming
  request is fully done, including when the server silently skips a
  requested path that doesn't exist.
- Per-connection requests are serialized one at a time (dropped the earlier
  `requestId` multiplexing) to match Obsidian core's own client exactly.
  Reconnects resume via a `lastKnownChangeId` baseline: the server streams
  just the intervening changes instead of a full rescan, backed by a new
  append-only `vault_change_log` journal wired into every file-metadata
  mutation site (including MCP's `write_note`/`delete_note`/`rename_note`).
- File pushes go through a single `push_req` op with content-hash dedup: if
  the exact content already lives elsewhere in the vault, the server
  hard-links/copies it onto the new path without asking for any bytes,
  matching Obsidian core's own `push` op.
- `size`/`purge`/`usernames` WS ops (no REST equivalent existed for the
  first two); `PurgeVault` now leaves a tombstone per live path so a stale
  client isn't told nothing changed.
- Per-file metadata (not just a bare "something changed" flag) carried in
  change-push notifications, and a per-file upload size cap
  (`maxFileSizeBytes`, default 2 GiB) enforced before any disk work.
- `pytest-cov`, with core `repository.py`/`service.py`/`ws_sync_resource.py`
  coverage driven from 71-85% to 99-100% (109 new tests).
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

- Every `SyncServiceServicer` RPC method (`Delta`/`Upload`/`Download`/
  `DownloadHistoryVersion`/`GetFileHistory`/`RestoreHistoryVersion`) now
  speaks plain `wire_types` dataclasses instead of protobuf `sync_pb2`
  messages, so it can run unmodified over both the (now removed) gRPC-Web
  path and the new WS path during the migration.
- `GetFileHistory`/`DownloadHistoryVersion`/`RestoreHistoryVersion` fully
  migrated off REST onto WS.
- All HTML moved out of `web.py` into Jinja2 templates.
- Server-side i18n migrated from a custom JSON-based system to Babel/gettext.
  Message ids are unchanged (the existing dotted keys), so no call site had
  to change; the fallback locale is now English instead of Korean.

### Removed

- The now-dead gRPC-Web/SSE/protobuf stack (`grpc_web_resource.py`,
  `streaming_upload_resource.py`, `watch_resource.py`, `streaming.py`,
  `sync_pb2*.py`) and the `grpcio`/`protobuf` dependencies, now that every
  RPC path runs over WS.
- The duplicate REST history endpoints and the admin streaming-stats
  endpoint (its counters were only ever incremented by the just-removed
  streaming-upload code).
- The old batch-oriented `upload_begin`/`file_chunk_header`/`file_chunk_eof`/
  `upload_ack` upload path (`UploadFiles`/`_finalize_uploaded_file` and
  ~700 lines of dedicated tests), superseded by the unified `push_req` op.

### Fixed

- A path-traversal vulnerability in `RestoreHistoryVersion`: a relative
  `..` in the restore target path could escape the vault directory because
  the escape check compared against the unresolved joined path instead of
  its `os.path.abspath()`'d form.
- Data loss / missing-table bugs in the owner-scoped and plugin-enablement
  schema migrations: both used to `DROP` the old table before recreating
  it, so a failure partway through could permanently lose rows or leave the
  table missing entirely. Fixed via a rename-aside pattern that never drops
  the original until its replacement is fully built and populated.
- `Delta` crashing on a tombstone the client hasn't caught up to yet (a
  dict-shape mismatch between a raw DB row and the 5-key shape every other
  merged-files entry uses).
- `GetFileHistory` was missing the `deleted`/`related_path` fields even
  though the DB columns existed, which would have broken the client's
  deletion/rename markers once it moved off REST.
- MCP's `write_note`/`delete_note`/`rename_note` mutated `file_metadata`
  outside the normal service path with no journal entry — `rename_note` in
  particular relabeled a row's path via an in-place `UPDATE`, so an
  MCP-driven rename was completely invisible to reconnect catch-up.
- A regression introduced while unifying the upload path: the per-file
  `maxFileSizeBytes` rejection was dropped, letting a client push an
  arbitrarily large file without any server-side size check.
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
