# portal-audit

Hermes plugin that streams every turn to Hermes Portal.

Hooks:

| Hook | Event | Blocking when the portal is down |
|---|---|---|
| `pre_llm_call` | `llm.pre` | yes, `HERMES_PORTAL_FAIL_CLOSED=1` |
| `post_llm_call` | `llm.post` | no |
| `pre_api_request` | `api.pre` | no (the turn gate already ran) |
| `post_api_request` | `api.post` | no |
| `pre_auxiliary_call` | `aux.pre` | no |
| `post_auxiliary_call` | `aux.post` | no |
| `pre_tool_call` | `tool.pre` | yes, `HERMES_PORTAL_FAIL_CLOSED=1` |
| `post_tool_call` | `tool.post` | no |
| `on_session_start` | `session.start` | no |
| `on_session_end` | `session.end` | no |

Environment:

```
HERMES_PORTAL_URL=http://127.0.0.1:9180
HERMES_PORTAL_TENANT=<tenant id>
HERMES_PORTAL_TOKEN=<tenant audit token>
HERMES_PORTAL_FAIL_CLOSED=1
HERMES_PORTAL_TIMEOUT=3
HERMES_PORTAL_MAX_PAYLOAD_BYTES=4194304
```

The plugin is inert when the URL/tenant/token are not all set. It uses only the
Python standard library and the `ctx.register_hook` surface, so it runs in both
`in_process` and `host` plugin isolation modes.

Payloads are JSON-safe and redact credential-shaped keys (`api_key`,
`authorization`, `password`, …) while keeping usage counters such as
`total_tokens`. Oversized payloads carry `_truncated: true` and the original
byte count. Events are chained per session with SHA-256; the portal verifies
the chain and rejects gaps.
