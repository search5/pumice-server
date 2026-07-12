# Pumice server

🇺🇸 English | [🇰🇷 한국어](README.ko.md)

Self-hosted sync/version-history/publish backend for the [Pumice](https://github.com/search5/pumice)
Obsidian plugin. Built on Python 3.13+ and Twisted (`asyncioreactor`): the sync RPCs (`Delta`,
`UploadFiles`, `DownloadFiles`, ...) are served by a native gRPC-Web `Resource`
(`src/server/grpc_web_resource.py`) driven directly off the reactor's event loop, and a Pyramid app
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
Translation strings live in `src/server/locale/{ko,en}.json`, not inline in `web.py`.

## What it does

- `/` — redirects to `/dashboard` or `/login` depending on whether a session cookie is present.
- `Delta` / `UploadFiles` / `DownloadFiles` (gRPC-Web) — the core file sync protocol.
- `GetFileHistory` / `DownloadHistoryVersion` / `RestoreHistoryVersion` (gRPC-Web) — per-file
  version history, backed by a physical backup (hard-linked where possible) on every change.
- `/api/*` (HTTP, Pyramid) — publish (upload/list/remove/download), publish sharing (invite by
  email, accept via invite code), version history REST mirrors, user accounts, device management,
  and the admin dashboard.
- `/publish/{username}/{vault}/...` — the actual published site, rendering markdown on the fly
  (wikilinks resolved, YAML frontmatter stripped).
