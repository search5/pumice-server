# Pumice server

🇺🇸 English | [🇰🇷 한국어](README.ko.md)

Self-hosted sync/version-history/publish backend for the [Pumice](https://github.com/search5/pumice)
Obsidian plugin. Built on Python 3.13+ and Twisted (`asyncioreactor`): the sync RPCs (`Delta`,
`UploadFiles`, `DownloadFiles`, ...) are served by a native gRPC-Web `Resource`
(`src/pumice_server/grpc_web_resource.py`) driven directly off the reactor's event loop, and a Pyramid app
handles the publish site, REST endpoints, and the web login/admin dashboard -- both share a single
HTTP port.

## Setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
cp .env.example .env   # edit as needed (DB type, port, data dir, admin credentials)
uv run server
```

`ADMIN_USER` and `ADMIN_PASSWORD` must be set in `.env` before the server will start -- there's no
other way to provision the first account. The server creates that admin account on first startup.

### Docker

Pull the pre-built image from GHCR (multi-arch: `linux/amd64` and `linux/arm64`):

```bash
docker run -d --name pumice-server -p 8080:8080 \
  --env-file .env \
  -v pumice-data:/data \
  ghcr.io/search5/pumice-server:latest
```

Or build it yourself from source:

```bash
docker build -t pumice-server .
docker run -d --name pumice-server -p 8080:8080 \
  --env-file .env \
  -v pumice-data:/data \
  pumice-server
```

`DATA_DIR` defaults to `/data` in the image (matching the `-v pumice-data:/data` volume above) --
everything that needs to survive a restart lives there: the DB (when `DB_TYPE=sqlite`), synced
vault content, version-history backups, and published sites. All other settings still come from
`.env`/`--env-file`, never baked into the image.

#### Docker Compose

`docker-compose.yml` pairs `pumice-server` with a CUBRID service on the same compose network:

```bash
docker compose up -d --build
```

This is a template for wiring an external DB correctly when pumice-server itself runs in a
container, not a pointer at any existing database. Inside a container, `127.0.0.1` (a sensible
`DB_HOST` when running directly on the host) means that container's own loopback -- not a sibling
container, and not the host. `docker-compose.yml` overrides `DB_HOST` to the CUBRID service's name
(`cubrid`), which resolves correctly over the compose network via Docker's built-in DNS. The rest
of `DB_*`/`ADMIN_*` still comes from `.env` via `env_file:`. The CUBRID service here starts out
empty -- to point at an existing external database instead, delete the `cubrid` service and set
`DB_HOST`/`DB_PORT` in `.env` (or `environment:`) to that database's actual address.

## Configuration (`.env`)

See `.env.example` for the full list. The important ones:

- `ADMIN_USER` / `ADMIN_PASSWORD` — required. Seeds the first (admin) account on first startup.
- `DB_TYPE`:
  - `sqlite` (default) — zero setup, a local file.
  - `mysql` / `mariadb` / `postgresql` / `cubrid` — point `DB_HOST`/`DB_PORT`/`DB_USER`/
    `DB_PASSWORD`/`DB_NAME` at an external database server.

## Accounts and authentication

There's no self-service sign-up. New accounts are created by an admin, either via the admin
dashboard or `POST /api/admin/users/create`. Once an account exists:

- There is exactly **one** kind of credential: a `device_tokens` row (token, username,
  device_name, created_at_ms). Every login — browser dashboard or Obsidian plugin — mints one.
  Logging in again from another device/browser doesn't invalidate earlier sessions; each is its
  own row, individually revocable.
- **Browser (web dashboard)**: `POST /user/login` sets the token as an `HttpOnly`,
  `SameSite=Lax` `session_token` cookie (`Secure` too, when served over HTTPS). The dashboard JS
  never touches the token directly — `fetch()` just relies on the browser sending the cookie.
  There's no "reissue my token" UI anymore; to end a session, log out (deletes that row) or revoke
  it from the device-management UI (see below).
- **Obsidian plugin**: the "Log in" button opens
  `/login?redirect=obsidian://pumice-auth&device_name=...` in the system browser; on success the
  page hands the token back via that `obsidian://` callback instead of the user copy/pasting one.
  The plugin then sends that same token for *everything* — gRPC sync metadata, and every HTTP call
  (Publish, version history) via `Authorization: Bearer`, `obs-token`, or a JSON-body `token`
  field, depending on the endpoint. The server accepts any of those, plus the cookie, uniformly
  (`extract_token()` in `web.py`, checked against `get_device_token()`).
- **Device management**: every account can see/revoke its own sessions at `/api/user/devices`
  (surfaced in the dashboard's "내 정보 관리" tab); admins can see/revoke *any* user's sessions at
  `/api/admin/users/{username}/devices` (surfaced per-user in the admin users table).
- Every vault belongs to exactly one account, with no cross-account bypass -- see "Vault identity"
  below for how that's enforced; `is_admin` only grants account-management capabilities
  (create/delete/reset other users), not access to their vaults.

## Vault identity

A vault's true identity is the pair `(owner_username, vault_id)`, not `vault_id` alone.
`vault_id` is just whatever the Obsidian client's vault is locally named (`vault.getName()`),
which isn't globally unique -- "Obsidian Vault" is literally Obsidian's own default vault name.
`owner_username` always comes from the caller's own authenticated identity, never from client
input, so a caller can only ever address vaults under their own name -- there's no "claim an
unowned vault_id" step and no cross-account lookup to get wrong. Every DB table
(`file_metadata`, `file_history`, `published_files`) and physical storage path
(`data_dir/{vaults,history,publish_meta,tmp}/{owner_username}/{vault_id}`, mirroring
`data_dir/published/{owner_username}/{vault_id}`) is scoped this way. `get_history_by_id()` also
takes `owner_username`, so a history row can't be fetched by ID across vault/account boundaries.

## Authorization

Permission checking is a real Pyramid ACL, not ad hoc per-view `if`s. `DeviceTokenSecurityPolicy`
(`web.py`) resolves the caller's identity from `extract_token()`; every view declares
`permission='authenticated' | 'admin' | 'vault-access'` on its `@view_config` and Pyramid enforces
it before the view body ever runs (a denial short-circuits to `forbidden_view`, which reproduces
the old 401-vs-403 split: no identity at all is 401, a real identity that's just not allowed here
is 403). The default permission is `'authenticated'` — a new route is private unless its view
explicitly opts out with `permission=NO_PERMISSION_REQUIRED`, the reverse of the old tween's
manually-maintained bypass-path list.

Vault-scoped routes (Publish, version history, admin's per-vault file view) use `VaultContext` as
their route `factory=`: it resolves `vault_id` from whichever the endpoint's calling convention is
(`matchdict`, query params, the `obs-id` header, or the JSON body), and sets `owner` to the
caller's own identity -- so `permission='vault-access'` is really just "are you logged in", with
`vault_id`/`owner` conveniently resolved onto `request.context` for the view to use.

## Language

The login and dashboard pages are translated server-side, negotiated per-request from the
browser's `Accept-Language` header (`ko` or `en` today) — no client-side language switching.
Translation strings live in `src/pumice_server/locale/{ko,en}.json`, not inline in `web.py`.

## What it does

- `/` — redirects to `/dashboard` or `/login` depending on whether a session cookie is present.
- `Delta` / `UploadFiles` / `DownloadFiles` (gRPC-Web) — the core file sync protocol.
- `GetFileHistory` / `DownloadHistoryVersion` / `RestoreHistoryVersion` (gRPC-Web) — per-file
  version history, backed by a physical backup (hard-linked where possible) on every change.
- `UploadFilesStream` (`/obsidian.sync.v1.SyncService/UploadFilesStream`, opt-in) — true
  client-streaming upload for browsers that support `fetch()` request-body streaming, handled
  entirely outside the gRPC-Web dispatch above (see "Streaming upload" below).
- `/api/*` (HTTP, Pyramid) — publish (upload/list/remove/download), publish sharing (invite by
  email, accept via invite code), version history REST mirrors, user accounts, device management,
  and the admin dashboard.
- `/publish/{username}/{vault}/...` — the actual published site, rendering markdown on the fly
  (wikilinks resolved, YAML frontmatter stripped).
- `/mcp` — an MCP (Model Context Protocol) server over Streamable HTTP, giving AI clients
  read/write access to a synced vault (see "MCP" below).

## MCP (Model Context Protocol)

`/mcp` exposes a synced vault to MCP-compatible AI clients (Claude Code, Claude Desktop, ...) over
Streamable HTTP (protocol version `2025-06-18`), via the official `mcp` Python SDK
(`src/pumice_server/mcp_resource.py`). Authentication reuses the exact same device token as
everything else — no separate MCP-specific credential.

### Connecting a client

```json
{
  "mcpServers": {
    "pumice": {
      "type": "http",
      "url": "https://your-server/mcp",
      "headers": { "Authorization": "Bearer <device-token>" }
    }
  }
}
```

Any device token works here — the same one the Obsidian plugin or a browser session already uses
(see "Accounts and authentication" above). A client sees only vaults belonging to that token's
account, same as everywhere else in this server.

### Tools

**Read-only:**

- `list_vaults` — list every vault this account has synced.
- `list_files` — current (non-deleted) files in a vault, with path/size/hash/modified time.
- `get_recent_notes` — the most recently modified files, newest first.
- `get_file_history` — version history for one file.
- `read_file_version` — content of one specific historical version, by `history_id`.
- `read_note` — a note's current content by path (one call instead of `get_file_history` +
  `read_file_version`).
- `search_notes` — full-text search across a vault's current files.
- `search_by_frontmatter` — find notes whose YAML frontmatter has a given key containing a given
  value.
- `list_published_files` — files currently published (shared publicly) from a vault.

**Write:**

- `write_note` — create or overwrite a note's current content.
- `delete_note` — soft-delete a note (recoverable via history, not a hard delete).
- `rename_note` — move/rename a note, preserving content and version history; refuses to
  overwrite an existing active note.

Full argument schemas are served via the standard MCP `tools/list` call — this list is intentionally
not duplicated in more detail here to avoid drifting out of sync with the actual code.

### End-to-end encryption

This server never holds an E2EE decryption key persistently — it has no record of whether a given
vault has E2EE enabled at all (that's a client-local setting, never transmitted). Every tool that
touches file content instead accepts an optional per-call `e2ee_password` argument:

- **Reads** (`read_file_version`, `read_note`, `search_notes`, `search_by_frontmatter`) decrypt on
  the fly if given a password, and let it fall out of scope once the response is built. Without
  one, content that fails to decode as UTF-8 text comes back as a clear error suggesting you retry
  with `e2ee_password` set (`search_notes`/`search_by_frontmatter` instead silently skip files they
  can't read as text, since scanning a whole vault should tolerate a handful of unreadable files).
- **`write_note`** can't afford to guess wrong the way a read can, since a bad guess immediately
  syncs out and corrupts the file for every other device. If `e2ee_password` is given, it's always
  trusted and used — the server encrypts the content the same way a real client would (same
  key-derivation and deterministic IV, verified byte-for-byte against real client output). If not
  given, it samples a few of the vault's existing files first and refuses the write, rather than
  silently storing plain text, if the vault looks encrypted.
- **`delete_note`/`rename_note`** never need a password — deleting only removes bytes, and renaming
  only moves them; neither touches file content.
- Metadata-only tools (`list_vaults`, `list_files`, `get_recent_notes`, `get_file_history`,
  `list_published_files`) are unaffected either way, since they never touch file content.

### Known limitations

Server-initiated notifications (the GET/SSE half of Streamable HTTP) aren't implemented yet — `GET
/mcp` returns `501`. There's currently nothing this server needs to push to a connected MCP client
proactively.

## Streaming upload (`UploadFilesStream`)

`UploadFiles` (above) buffers the whole request body in memory before processing any of it --
fine for its intended batch sizes, but not something a much larger single request should rely
on. `UploadFilesStream` is an opt-in alternative that parses the request body incrementally as
bytes arrive (`src/pumice_server/streaming.py`'s `EnvelopeStreamParser` and
`src/pumice_server/streaming_upload_resource.py`'s
`StreamingUploadRequest`/`StreamingUploadResource`), so files land on disk and get
hashed/backed up/recorded exactly like `UploadFiles`, without ever buffering the whole body in
memory. It's reached by a hand-rolled `fetch()` + envelope framing on the client rather than the
generated gRPC-Web stub, since browsers' grpc-web/connect-es libraries don't support
client-streaming at all.

Guarantees:

- **Auth-before-streaming** — the `Authorization` header is resolved to a device/owner identity
  before a single body byte is handed to the parser. An invalid/missing token gets a bare `401`
  and the connection is dropped without the (possibly attacker-controlled) body ever being
  processed.
- **Backpressure** — every piece of blocking work (token resolution, each frame's disk I/O)
  pauses the transport until it drains, so a fast or malicious sender can't queue arbitrarily far
  ahead of what's actually been written to disk.
- **Blocking I/O avoidance** — token resolution and all file I/O run via
  `twisted.internet.threads.deferToThread`, matching the `asyncio.to_thread` convention
  `service.py` already uses for the same operations on `UploadFiles`, so the reactor never
  blocks on upload I/O.

### Timeouts & observability

- **Auth-resolution timeout** (`AUTH_TIMEOUT_SECONDS = 10`) — a stuck auth lookup (DB pool
  exhaustion, deadlock) errors out with `503` rather than hanging the request and its paused
  transport indefinitely, distinct from a `401` ("too slow" vs. "wrong credentials").
- **Max-upload-duration ceiling** (`MAX_UPLOAD_SECONDS = 600`) — an activity-independent ceiling
  on the whole request, separate from the idle timeout below, so a sender that drips bytes just
  often enough to never go idle still can't hold a connection open indefinitely.
- **Global idle timeout** (60s) — set explicitly via `Site(root_resource, timeout=60)` in
  `main.py`, applied to every connection through Twisted's `HTTPFactory`/`HTTPChannel`.
- **Metrics** (`StreamingUploadMetrics`) — active/total uploads, bytes received, file
  success/failure counts (broken down by failure reason: invalid vault/path, temp-file error,
  path mismatch, hash mismatch), rejection counts (auth failed/timed out, frame too large,
  malformed frame, max duration exceeded), and backpressure pause frequency/total duration.
  Exposed via `GET /api/admin/streaming-stats` (`permission='admin'`) and a periodic log summary
  (`main.py`'s `log_streaming_upload_stats`).

## Support

If you'd like to sponsor this project, reach out at search5@gmail.com. Sponsorships make a real
difference in how much time can go into development.
