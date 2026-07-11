# Pumice server

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
  - `json` — a flat JSON metadata store, no DB server needed.
  - `mysql` / `mariadb` / `postgresql` / `cubrid` — point `DB_HOST`/`DB_PORT`/`DB_USER`/
    `DB_PASSWORD`/`DB_NAME` at an external database server.

## Accounts and authentication

There's no self-service sign-up. New accounts are created by an admin, either via the admin
dashboard or `POST /api/admin/users/create`. Once an account exists:

- Logging in at `/login` (or `POST /user/login`) issues **two** independent credentials:
  - `token` — this account's single web-dashboard token, used for the HTTP/publish API.
  - `device_token` — a fresh, per-login credential for gRPC sync, recorded in the `device_tokens`
    table. Logging in from a second device doesn't invalidate the first device's token.
- The Obsidian plugin's "Log in" button opens `/login?redirect=obsidian://pumice-auth&device_name=...`
  in the system browser; on success the page hands the new `device_token` back to the plugin via
  that `obsidian://` callback instead of the user copy/pasting a token.
- Every vault is owned by exactly one account (first caller to touch a given vault ID claims it,
  admin included) -- there is no cross-account bypass, including for admins; `is_admin` only grants
  account-management capabilities (create/delete/reset other users), not access to their vaults.

## What it does

- `Delta` / `UploadFiles` / `DownloadFiles` (gRPC-Web) — the core file sync protocol.
- `GetFileHistory` / `DownloadHistoryVersion` / `RestoreHistoryVersion` (gRPC-Web) — per-file
  version history, backed by a physical backup (hard-linked where possible) on every change.
- `/api/*` (HTTP, Pyramid) — publish (upload/list/remove/download), version history REST mirrors,
  user accounts, and the admin dashboard.
- `/publish/{username}/{vault}/...` — the actual published site, rendering markdown on the fly
  (wikilinks resolved, YAML frontmatter stripped).
