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

## Streaming upload (`UploadFilesStream`)

`UploadFiles` (above) buffers its whole request body into memory before processing a single
byte (`request.content.read()` in `grpc_web_resource.py`) -- fine for the batch sizes it's
tuned for, but not something a much larger single request should rely on. `UploadFilesStream`
is a second, opt-in path to the same effect (files land on disk, get hashed/backed up/recorded
exactly like `UploadFiles`) that instead parses the request body incrementally as bytes arrive,
via `src/pumice_server/streaming.py` (`EnvelopeStreamParser`) and
`src/pumice_server/streaming_upload_resource.py` (`StreamingUploadRequest`/`StreamingUploadResource`).
It's reached by a hand-rolled `fetch()` + envelope framing on the client, not the generated
gRPC-Web stub -- browsers' grpc-web/connect-es libraries don't support client-streaming at all.

This was TDD-developed and real-socket-verified as a standalone PoC first
(`/home/jiho/twisted-streaming-poc`, 28 tests) before being ported in here with three
production concerns layered on top (`tests/test_streaming_upload_request.py`,
`tests/test_upload_accumulator.py`, 17 more tests):

- **Auth-before-streaming** — the `Authorization` header is resolved to a device/owner
  identity *before* a single body byte is handed to the parser (headers are fully parsed by
  the time Twisted calls `gotLength()`, ahead of any body byte). An invalid/missing token
  gets a bare `401` and the connection is dropped without ever processing the (possibly
  attacker-controlled) body.
- **Backpressure** — every piece of blocking work (token resolution, each frame's disk I/O)
  pauses the transport (`stopReading()` on the real TCP transport) until it drains, so a
  fast/malicious sender can't queue arbitrarily far ahead of what's actually been written to
  disk.
- **Blocking I/O avoidance** — token resolution and all file I/O run via
  `twisted.internet.threads.deferToThread`, matching the `asyncio.to_thread` convention
  `service.py` already uses for the same operations on `UploadFiles`. Verified for real: a
  slow trickled upload running concurrently with 259 `Ping` calls on a separate connection
  kept every `Ping` under 5ms — the reactor never stalled waiting on the upload's disk I/O.

One real bug found only by driving this against an actual running server (not just
`StringTransport`-based tests): a request whose entire (small) body arrives before the
threaded auth check completes used to race ahead into `render()` with `owner_username` still
unset, since `pauseProducing()` only blocks *future* socket reads, not bytes already delivered
within the current `dataReceived()` call. Fixed by deferring `requestReceived()` itself until
auth actually resolves (see `StreamingUploadRequest._onAuthResolved`/`_onAuthFailed`), with a
regression test (`test_small_full_body_arriving_before_auth_resolves_does_not_race_render`)
covering it. A second bug from the same real run: acks were being written as each file
finished, mid-request -- which corrupts the response, because this is a plain HTTP/1.1
connection (the app server never terminates TLS/HTTP2 itself) and a response can't start
until the whole request has been received, regardless of what Twisted's `Request.write()`
technically allows you to call early. Acks are now buffered and flushed in one batch from
`render_POST()`, exactly matching `UploadFiles`' existing ack-after-full-body behavior.

### Timeouts & observability

Two more production concerns, added after the above (48 tests total for this feature now):

- **Auth-resolution timeout** (`AUTH_TIMEOUT_SECONDS = 10`) — a stuck DB thread call
  (pool exhaustion, deadlock) no longer hangs the request (and its paused transport) forever;
  it errors out with `503`, distinct from `401` ("we were too slow" vs "your credentials are
  wrong"). Note this only stops *this Deferred* from waiting further — `deferToThread` can't
  interrupt an actually-still-blocked worker thread.
- **Max-upload-duration ceiling** (`MAX_UPLOAD_SECONDS = 600`) — Twisted's own idle timeout
  (see below) resets on every byte received, so it doesn't catch a slow-loris-style sender
  that drips bytes just often enough to never go idle but never finishes. This is a separate,
  activity-independent ceiling on the whole request, scheduled in `gotLength()` and cancelled
  once the body fully arrives or the request aborts for any other reason.
- **Global idle timeout** — turns out this already existed (`Site`/`HTTPFactory.__init__`
  defaults `timeout=60`, applied to every `HTTPChannel` via `buildProtocol()`) — confirmed by
  reading the Twisted source rather than trusting an initial assumption that it was `None`
  (that was checking `HTTPChannel.timeOut`'s *class* default, not the actual per-instance
  value the factory sets). Now spelled out explicitly in `main.py` (`Site(root_resource,
  timeout=60)`) so it's a documented decision rather than an implicit library default.
- **Metrics** (`StreamingUploadMetrics`, no new dependency — there's no existing metrics
  stack here) — active/total uploads, bytes received, file success/failure counts (with
  failure broken down by reason: invalid vault/path, temp-file error, path mismatch, hash
  mismatch), rejection counts (auth failed/timed out, frame too large, malformed frame, max
  duration exceeded), and backpressure pause frequency/total duration. Exposed via
  `GET /api/admin/streaming-stats` (`permission='admin'`, matching the existing admin API
  convention) and a 5-minute log summary (`main.py`'s `log_streaming_upload_stats`,
  LoopingCall-based like the existing temp-file GC task).

A second real deadlock bug turned up while wiring metrics in: `_UploadAccumulator._enqueue()`
used `addCallback` for its inflight/`resumeProducing()` bookkeeping and a separate
`addErrback` only for logging -- meaning an *unexpected* exception in a blocking handler (an
`OSError` writing to a full disk, say) skipped the bookkeeping entirely and left the
transport paused forever. Fixed by merging both into one `addBoth`, with a regression test
(`test_unhandled_exception_in_blocking_handler_still_resumes_transport`) that fails (hangs)
without the fix.

One more thing verified empirically rather than assumed: `Request.notifyFinish()` (used to
track `active_uploads`) does *not* fire under a `StringTransport`-driven test purely from
calling `loseConnection()` -- confirmed by direct experiment, then fixed at the *test* level
(not the implementation) by explicitly simulating `channel.connectionLost()`, matching how
this actually completes under a real reactor/socket.

## Support

If you'd like to sponsor this project, reach out at search5@gmail.com. Sponsorships make a real
difference in how much time can go into development.
