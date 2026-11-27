# Hermes Portal

Hermes Portal is the multi-user gateway and audit spine for a Hermes Agent deployment.

Current slice (MVP):

- account login with `admin` / `member` / `viewer` roles
- tenant tokens for each Hermes runtime
- append-only audit store with per-session hash chains
- live SSE feed, session replay, chain verification
- tenant and user administration
- `portal_audit` Hermes plugin that ships turns, model requests, auxiliary calls, tool calls and results

It is deliberately a separate service. Hermes core stays close to upstream; the portal owns identity, tokens, and the audit record.

## Quick start (Windows, from the repo root)

```powershell
$env:PORTAL_ADMIN_PASSWORD = "change-me-please"
$env:PORTAL_DATA_DIR = "$PWD\portal\data"
.\.venv\Scripts\python.exe -m uvicorn portal.app:app --host 127.0.0.1 --port 9180
```

Open `http://127.0.0.1:9180`, log in as `admin` with the password you set.

If `PORTAL_ADMIN_PASSWORD` is omitted on the first start, the portal generates one and writes it to `portal/data/initial-admin.txt`.

## Configure a tenant and install the audit plugin

1. In the portal, open **租户** → create a tenant. Copy the token shown once.
2. Install the plugin into a Hermes home:

```powershell
.\.venv\Scripts\python.exe portal\install_plugin.py --hermes-home "C:\path\to\hermes-home"
```

Or copy the `portal_audit/` directory to `<HERMES_HOME>\plugins\portal-audit\` yourself.

User plugins are opt-in. Enable it once for that Hermes home:

```powershell
hermes plugins enable portal-audit
```

If `plugins enable` insists on provisioning a managed Python and that download is unavailable offline, add the opt-in to `config.yaml` instead:

```yaml
plugins:
  enabled:
    - portal-audit
```

3. Start Hermes with the tenant environment:

```powershell
$env:HERMES_PORTAL_URL = "http://127.0.0.1:9180"
$env:HERMES_PORTAL_TENANT = "the-tenant-id"
$env:HERMES_PORTAL_TOKEN = "the-token-shown-once"
$env:HERMES_PORTAL_FAIL_CLOSED = "1"
```

Every turn, model request/response, auxiliary model call, tool call, and tool result now appears in the console. With `HERMES_PORTAL_FAIL_CLOSED=1` (default), a turn or tool call is blocked if the audit event cannot be accepted.

## Member chat (the portal is the only web entry)

1. In **租户 → 租户运行环境**, point the tenant at an internal Hermes API server:
   - **API Base URL**: e.g. `http://127.0.0.1:8642` or `http://tenant-id:8642` on the internal Docker network
   - **API Key**: that runtime's `API_SERVER_KEY`
   - **模型名**: the model alias the API server advertises (default `hermes-agent`)

   Press **测试连接** (it calls `/v1/models`). Do not publish the tenant's `8642` port; only the portal should reach it.
2. Create a member in **用户** and choose a tenant, or use **授权已有用户**. Members can chat; viewers can read their own conversations but cannot send.
3. Sign in as the member. The browser talks only to the portal; the portal streams `/v1/chat/completions` from the tenant and stores the user/assistant messages. The conversation id is sent as `X-Hermes-Session-Id`, so the audit events and the chat share one session.

Runtime API keys are encrypted at rest with `portal/data/runtime.key`. Back that file up with the database; losing it requires re-entering the runtime keys.

### Attachments

The member composer accepts drag-and-drop or the paperclip button: up to 5 files, 25 MB each and 50 MB total. Executable extensions are refused. Files are stored under `PORTAL_DATA_DIR/uploads/<tenant>/<conversation>/` and the agent receives their absolute paths in the user message, so it can analyze them with terminal/Python or file tools. Attachment chips are persisted with the message.

This is host storage for the MVP. Per-tenant containers will mount the uploads directory; do not let untrusted users upload sensitive files until that isolation lands.

### Dev demo without model tokens

```powershell
.\.venv\Scripts\python.exe -m uvicorn portal.dev_fake_hermes:app --host 127.0.0.1 --port 8643
```

Then configure a tenant runtime with `http://127.0.0.1:8643`, key `sk-fake`, model `demo-model`. The fake server only streams a canned reply; it does not run tools or emit audit events, so use a real Hermes API server to test the full audit chain.

## Docker stack

`docker-compose.portal.yml` runs the portal, an allowlisted egress proxy, and one tenant Hermes container:

- `hermes-portal` publishes `9181:9180`; it joins an internal tenant network and a public bridge.
- `hermes-tenant-visitor` has **no published ports** and joins only the internal network.
- `hermes-egress` joins the internal network plus an egress bridge and forwards only `/v1/...` to the configured model upstream. Requests are logged with model/status/duration; bodies and Authorization are not logged.
- `hermes-egress-web` is a logged HTTP/HTTPS forward proxy for web browsing, package installs and data collection. It blocks loopback/private/metadata addresses (SSRF guard) and logs destination host, port and byte counts, never paths or bodies. `WEB_EGRESS_ALLOW_ALL=1` permits public internet; set it to `0` and `WEB_EGRESS_ALLOW_HOSTS=example.com,*.example.org` for a domain allowlist.
- Tenant uploads are mounted at `/uploads` in both portal and tenant.

```powershell
docker compose -f docker-compose.portal.yml up -d --build
docker compose -f docker-compose.portal.yml ps
docker logs -f hermes-egress
docker compose -f docker-compose.portal.yml down
```

The internal tenant network blocks all direct internet and host access. Model traffic goes through `hermes-egress`; general web traffic (browser, curl, pip) goes through `hermes-egress-web` via `HTTP_PROXY`/`HTTPS_PROXY`. Neither proxy is published on the host. The current web policy is logged-open public internet with private-IP blocking; switch to an allowlist with the `WEB_EGRESS_ALLOW_*` variables. The workstation VPN's fake-DNS range `198.18.0.0/15` is explicitly allowed for this local setup; remove or adjust `WEB_EGRESS_ALLOW_CIDRS` in production.

## Business-safe tenant

`tenant-business` is the non-technical colleague profile:

- `platform_toolsets.api_server` is explicitly limited to `web`, `file`, `browser`, `vision`, `skills`, `todo`, `memory`, `session_search`; no `terminal`, `code_execution`, `delegation`, or `image_gen`.
- `SOUL.md` states that the assistant has no shell/code execution so it does not claim those capabilities.
- The tenant's `WORKBUDDY_API_KEY` is the placeholder `portal-managed`; the real model credential lives only in `hermes-egress` (`EGRESS_API_KEY`) and is injected on the way out. A colleague with file/shell access cannot read or reuse the model key.
- The `business` portal tenant / `colleague` account is separate from the full-capability `visitor` tenant.

The `business-guard` plugin blocks file tools from the tenant's `.env`, `config.yaml`, `auth.json`, `state.db`, sessions, plugins, memories and logs, and blocks browser access to `file://` and internal service URLs. User work stays under `/uploads` (or other non-sensitive paths).

### Audit heartbeat

Every tenant's `portal-audit` plugin posts `/api/tenants/heartbeat` on load and every 30 seconds. The chat proxy refuses a send when the tenant has no heartbeat or its last heartbeat is older than 180 seconds. This makes "no audit plugin" a hard failure instead of a silent gap.

### Business guard

The business tenant also loads `business-guard` as a `pre_tool_call` hook. It blocks file access to Hermes config, credentials and audit state, and blocks browser URLs pointing at `file://` or internal services (`portal`, `egress`, loopback). It does not remove regular file tools for `/uploads` or normal project files. Writes are limited to `/uploads/business` plus the tenant's own `skills/` and `memories/` directories.

## Server-side skill library

Skills live with the portal (`portal/skills/`), not in each tenant home:

- `catalog.json` lists id, display name, description, allowed roles/tenants and the Markdown entry file.
- The chat sidebar shows the visible skills. Selecting one injects its Markdown into that turn's user
  context; the conversation stores only the skill IDs in message metadata.
- The agent still uses its normal tenant tools. Real CSV/Excel computation goes through the
  server-side DataLab worker, not tenant code execution.
- Skills are versioned with the repo and audited like any other model input. Add a new skill by
  adding its Markdown file and catalog entry, then rebuild the portal.
- Included examples: `data-analysis`, `database-analysis` and `web-research`. Skills are
  role/tenant-scoped; per-user skill overrides are a later layer.

### Scoped memories

Shared business tenants must not use Hermes's tenant-global native memory. The
`portal_memory/` plugin replaces it with two portal scopes:

- `private`: keyed by the conversation owner; only that portal user sees it.
- `team`: keyed by tenant; every member sees it, but only an admin can add/delete it.

Install it in the shared tenant and turn off the native memory toolset:

```text
copy portal_memory/ to <HERMES_HOME>/plugins/portal-memory/
hermes plugins enable portal-memory
hermes tools enable --platform api_server portal_memory
hermes tools disable --platform api_server memory
```

The chat sidebar has a memory manager; private and team memories are injected into the
turn context with clear labels. The agent gets `memory_list` / `memory_add` tools.
This does not yet split learned skills per user; skills remain role/tenant-scoped.

### DataLab

The `datalab` service runs pandas + DuckDB in an isolated container with a read-only uploads
mount and no host ports. The tenant gets one tool, `datalab_query`, from `portal_datalab/`:

```text
copy portal_datalab/ to <HERMES_HOME>/plugins/datalab/
hermes plugins enable datalab
hermes tools enable --platform api_server datalab
```

The tool forwards only a conversation id, up to 10 file paths inside that conversation workspace,
one SELECT/WITH query, and a row limit. The portal checks tenant token + workspace path, then calls
DataLab with `DATALAB_TOKEN`. DataLab rejects writes, DDL, multiple statements, file-reading table
functions and external database functions.

Database connectors live in `datalab/connections.json` and never enter the tenant:

- `datalab_databases` returns only connection ids/type/description allowed for the **conversation
  owner's portal role**; `datalab_db_query` runs one read-only SELECT/WITH against that connection.
- The shipped demo seeds `sales_demo` (admin + member) and `finance_demo` (admin only) into a
  Docker volume when `DATALAB_SEED_DEMO=1`.
- Set `DATALAB_SEED_DEMO=0`, replace the demo connections with real read-only connections, and add
  a Postgres/MySQL adapter in `datalab/app.py` for production. Keep passwords in server-side config
  or a secret store, never in the browser or tenant prompt.
- This is connection-level role gating. Metric-level permissions, row-level filters and a proper
  data-role model are the next layer; do not expose a production database until those are defined.

## Local folder bridge

A member can authorize one local directory per conversation from the chat sidebar (`连接文件夹`). Chromium browsers only (Edge/Chrome); the portal must be served over HTTPS or localhost because the File System Access API needs a secure context.

- The browser uploads the selected directory into the conversation's server mirror under `/uploads/<tenant>/<conversation>/local`.
- The agent reads and writes that mirror with normal file tools; each user turn carries a note naming the mirror path.
- `同步回本地` downloads server changes and writes them through the still-authorized directory handle. SHA-256 baselines detect files changed locally too and the user is asked before overwriting.
- Limits: 500 files, 50 MB per file, 200 MB per folder. `.git`, `node_modules`, virtualenvs, `.env`, private keys and credential files are skipped.
- `断开` deletes the server mirror and clears the browser handle; local files are untouched.
- File contents processed by the agent enter the normal audit trail.

The directory handle is stored in IndexedDB per conversation. After a browser restart the user may need to click `重新授权` once; the server mirror and baseline remain.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PORTAL_DATA_DIR` | `portal/data` | SQLite database and bootstrap password file |
| `PORTAL_DB` | `<data>/portal.db` | Explicit database path |
| `PORTAL_ADMIN_USER` | `admin` | Bootstrap admin username |
| `PORTAL_ADMIN_PASSWORD` | generated | Bootstrap admin password, only used when no users exist |
| `PORTAL_SESSION_TTL_SECONDS` | 43200 | Browser session lifetime |
| `PORTAL_SECURE_COOKIES` | `0` | Set to `1` behind HTTPS |
| `PORTAL_MAX_BODY_BYTES` | 8 MiB | Maximum audit ingest request |
| `PORTAL_LOGIN_MAX_ATTEMPTS` | 8 | Login throttle per IP + username |
| `PORTAL_LOGIN_WINDOW_SECONDS` | 300 | Login throttle window |
| `DATALAB_URL` | `http://datalab:8090` | Internal DataLab worker URL |
| `DATALAB_TOKEN` | empty | Shared portal/worker token; required for analysis |

## CLI

```powershell
.\.venv\Scripts\python.exe -m portal.cli create-user alice "strong-password" member
.\.venv\Scripts\python.exe -m portal.cli create-tenant "研发一组" --id rd-team-1
.\.venv\Scripts\python.exe -m portal.cli list
.\.venv\Scripts\python.exe -m portal.cli verify rd-team-1 <session-id>
.\.venv\Scripts\python.exe -m portal.cli seed-demo
```

`seed-demo` creates a demo tenant and a synthetic audited session so you can see the console before wiring a real Hermes runtime.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s portal\tests -t .
.\.venv\Scripts\python.exe -m unittest discover -s portal_audit\tests -t .
```

## Security model

- The portal cookie is `HttpOnly` + `SameSite=Lax`; admin writes require a per-session CSRF header.
- Tenant tokens are stored as SHA-256 hashes and shown once.
- Audit events are chained per `(tenant, session_id)`; the portal rejects gaps, duplicate `seq` with different content, and bad hashes.
- The console only exposes audit data to `admin`. Members use `/chat`; viewers can read their own conversations but cannot send.
- Audit payloads redact credential-shaped keys but keep conversation content. Add a retention policy before production use.
- Fail-closed is enforced at turn start (`pre_llm_call`) and before each tool call (`pre_tool_call`). If the portal dies mid-turn, a text-only turn can finish with a missing tail; the next turn is blocked. Keep the portal on the same host or a reliable internal network for strict deployments.
- Run one uvicorn worker for the MVP. The login limiter and SQLite are single-process; move to Postgres + a shared limiter before scaling out.

## Current limitations

- Tenant containers are not provisioned yet. The portal chat proxy is the only web entry, but an operator still has to start the Hermes runtime and install the audit plugin.
- Egress is not yet default-deny and there is no container-per-user orchestration; both are the next slice.
- Approval requests are forwarded to the member UI as a notice. The admin approve/deny controls are not wired yet; a runtime that requires approval will wait.
- There is no runtime heartbeat enforcement yet. A runtime that runs without `portal-audit` can serve chat, but the admin console will show no audit events for it. Do not configure such a runtime in production.
- No object storage/compaction for very large transcripts yet.
