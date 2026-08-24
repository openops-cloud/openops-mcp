# OpenOps MCP server — transports and authentication

> **Superseded.** This is the original design record, kept for its rationale only. It
> describes a route allow-list and a "no project state" rule that no longer match the code.
> For how the server works now, read [architecture.md](./architecture.md); for running it,
> the [README](../README.md). This file is expected to be removed.

**Status:** approved, not yet implemented
**Linear:** OPS-4673
**Counterpart:** the OAuth 2.1 authorization server in `openops-cloud/openops`
(`docs/oauth-design.md` there). This document covers only the MCP resource server.

## Problem

This server exposes a filtered set of OpenOps API routes as MCP tools. Today it
runs one way: spawned as a stdio subprocess by the Node API for the built-in AI
chat, authenticating with a single `AUTH_TOKEN` environment variable. The route
list is hardcoded in `main.py`, and FastMCP is pinned at 2.7.1 — two major versions
behind.

External agents (Claude Code, Codex, Claude.ai and ChatGPT connectors, M365 Copilot)
need to reach the same tools over HTTP, each as a different user, without anyone
sharing a static credential.

## Requirements

1. Two transports, **identical tool surface**. The only difference is how a request
   is authenticated and how the downstream API call is authorized.
2. `stdio` keeps working exactly as it does now: spawned by the API, one
   `AUTH_TOKEN`, one user per process.
3. `http` is OAuth-protected and serves many users concurrently, with no shared
   per-user state.
4. Tools are generated from the API's live OpenAPI document.
5. Which routes become tools is supplied at deployment or start — not compiled in.
6. Current FastMCP and a dependency set that reflects what the code actually uses.

## Shape

Because the two modes differ *only* in authentication, the server is built once and
handed an auth strategy. Everything about tool generation — fetching the spec,
filtering it, naming tools — is shared, so the two modes cannot drift apart.

```
openops_mcp/
  config.py          env → typed settings; fails fast with actionable messages
  routes.py          load and validate the route allow-list
  openapi.py         fetch the spec, prune it, build route maps
  server.py          build the FastMCP instance from a spec + auth strategy
  auth/
    static.py        stdio: one bearer token for every downstream call
    oauth.py         http: local JWT verification + protected-resource metadata
    exchange.py      RFC 8693 token exchange — cached, fail-closed
  logging_config.py
main.py              entrypoint: read config, pick a transport, run
```

The current single module cannot be tested without a live API, and the two auth
paths are the part most worth testing. That is the reason for the split; it is not
layering for its own sake.

## Tool generation

The spec is fetched from `OPENOPS_API_OPENAPI_URL` (default
`<api>/v1/openapi/json`). That route is public in the API, so no credential is
needed to read it — the server can build its tool list before it has any user
context, which is what lets both modes share the surface.

### The route allow-list

A YAML (or JSON) file, path in `OPENOPS_MCP_ROUTES`:

```yaml
routes:
  - path: /mcp/flows/
    methods: [get, post]
  - path: /mcp/flows/{id}/version
    methods: [get]
    name: get_flow_version # optional; overrides the generated tool name
  - path: /v1/app-connections/
    methods: [get, patch]
```

Validated at startup, refusing to start on:

- a missing or unparseable file;
- an entry with no path or no methods;
- **an entry that matches nothing in the fetched spec.** This is the important one:
  today a mistyped path silently produces no tool, and the failure surfaces later as
  an agent that cannot do something. Startup lists the unmatched entries instead.

Routes present in the spec but absent from the file are logged at debug level, so
newly added endpoints are discoverable without reading the API source.

### Filtering is two layers, deliberately

The spec dict is pruned to exactly the allowed path and method pairs before FastMCP
sees it, which keeps the tool set exact and the document small. A trailing
`RouteMap(mcp_type=MCPType.EXCLUDE)` catch-all is then added, because FastMCP's
default is to turn *every* remaining route into a tool — if pruning ever misses
something, the catch-all stops it quietly becoming an exposed tool.

Optional `name` and `description` overrides from the file are passed through
`mcp_names` and `mcp_component_fn`.

## Authentication

### stdio

`AUTH_TOKEN` is set on every downstream request. Unchanged behaviour; the API mints
a short-lived `SERVICE` token per chat request and spawns the process with it.

### http

Inbound tokens are verified **locally** against the authorization server's JWKS —
no round trip per request:

```python
JWTVerifier(
    jwks_uri=f"{issuer}/v1/oauth/jwks.json",
    issuer=issuer,
    audience=resource_url,  # this server's canonical URI
    required_scopes=["mcp"],
)
```

wrapped in `RemoteAuthProvider(token_verifier=…, authorization_servers=[issuer],
base_url=resource_url)`, which serves the RFC 9728 protected-resource metadata and
the `WWW-Authenticate` challenge on 401. FastMCP provides both; they do not need to
be hand-written.

The client's token is **never** forwarded to the API. Per tool call, the verified
token is exchanged for a separate API-audience token:

```
POST {issuer}/v1/oauth/token
Authorization: Basic base64(openops-mcp-rs:{OPENOPS_MCP_CLIENT_SECRET})
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token={the client's token}
```

Two properties matter here:

- **Cached**, keyed by `(sha256(subject token), project_id)` for
  `min(remaining subject lifetime, 60s)`. Without this every tool call costs an
  authorization-server round trip.
- **Fail-closed.** If the exchange fails, the tool call fails with an
  authentication error. It must never fall through to an unauthenticated request —
  which produces a confusing downstream 401 and hides the real cause.

`stateless_http=True` is passed to the transport: each request is independent, so
the token used downstream is the one on the current request rather than whichever
arrived first in a session.

### No project state

The token carries `project_id`; the exchanged token inherits it; the API resolves
it. There is no active-project tool and no per-user map in this process. That is
what makes multi-user safe — two users' requests share nothing — and it is the same
decision recorded in the authorization server's design, where mutable project state
was removed rather than relocated.

Reading identity uses the verified token (`get_access_token().claims`), never an
unverified decode of the JWT payload.

## Configuration

| Variable | Mode | Purpose |
| --- | --- | --- |
| `MCP_TRANSPORT` | both | `stdio` (default) or `http` |
| `OPENOPS_API_URL` | both | Base URL for downstream API calls |
| `OPENOPS_API_OPENAPI_URL` | both | Spec location; defaults from `OPENOPS_API_URL` |
| `OPENOPS_MCP_ROUTES` | both | Path to the route allow-list |
| `AUTH_TOKEN` | stdio | Bearer token for every downstream call |
| `OPENOPS_MCP_ISSUER` | http | Authorization server base URL |
| `OPENOPS_MCP_RESOURCE_URL` | http | This server's canonical URI, and the expected token audience |
| `OPENOPS_MCP_CLIENT_SECRET` | http | Resource-server credential; client id is `openops-mcp-rs` |
| `MCP_HTTP_HOST`, `MCP_HTTP_PORT` | http | Bind address |
| `LOGZIO_TOKEN`, `ENVIRONMENT` | both | Optional log shipping |

Every value is read once into a typed settings object and validated there, so a
misconfiguration fails at startup with a message naming the variable rather than at
the first request.

The resource URL must match the authorization server's `OPS_MCP_RESOURCE_URL`
exactly. That server refuses to boot if its value equals its own issuer, because
equal audiences would let this server accept tokens minted for the API.

## Dependencies

FastMCP `3.4.5` (current stable), Python `>=3.10` (FastMCP's floor). `uv` with a
committed `uv.lock` is the single source of truth; `requirements.txt` is removed.

Direct dependencies reduce to what the code imports: `fastmcp`, `httpx`,
`python-dotenv`, `pyyaml`, `logzio-python-handler`. Dropped: `fastapi`, `uvicorn`,
`pydantic`, `redis`, `openai`, `setuptools`, `wheel` — none are imported, and
`uvicorn`/`pydantic` arrive transitively through `fastmcp[server]`. `requests` is
dropped too, since `httpx` is already a dependency and can fetch the spec.

### What the 3.x upgrade breaks

Verified against the installed package, not the documentation:

- `from_openapi()` no longer accepts `all_routes_as_tools` or `default_headers`.
  Filtering is expressed with route maps; downstream headers come from the client.
- `RouteMap` and `MCPType` move to `fastmcp.server.providers.openapi`.
- Transport settings (`host`, `port`, `stateless_http`) move from the constructor to
  `run()`.
- Auth providers no longer read environment variables implicitly; values are passed
  explicitly.

## Testing

The repository currently has no tests. Adding them is part of this work, because
the route-filtering and token-exchange logic is exactly what a reviewer cannot
verify by reading.

- **Unit**: route-file parsing and validation, including the "listed route is not in
  the spec" failure; spec pruning; route-map construction; exchange caching and
  fail-closed behaviour; settings validation.
- **Integration**: drive the assembled server in-process with `fastmcp.Client`
  against a running OpenOps API. Assert the tool list equals the allow-list, that an
  unauthenticated HTTP request gets a 401 carrying the resource-metadata challenge,
  that an authorized call reaches the API, and that **two concurrent users receive
  distinct downstream tokens** — the property that makes multi-user correct.

## Deployment

A `Dockerfile` in this repository builds one image serving both modes, defaulting to
`MCP_TRANSPORT=http`; the same package still runs as a stdio subprocess when the API
spawns it. Multi-stage, `uv sync --frozen`, non-root.

The Helm chart lives in `openops-cloud/helm-chart` and is not edited here. It needs:
the image and replica count; the OAuth variables above with the client secret from a
`Secret`; the route allow-list mounted from a `ConfigMap`; and path routing that
sends `/mcp` and `/.well-known/oauth-protected-resource` to this service while
`/v1/oauth/*` continues to the API.

## Risk to settle first

Injecting a per-request token into OpenAPI-derived tools relies on an `httpx`
request hook calling `get_access_token()`, which only resolves if FastMCP executes
the tool inside the request's async context. An earlier prototype hit precisely this
and needed `stateless_http`. The first implementation task therefore proves it with
two concurrent users before anything is built on top; the assertion becomes a
permanent test rather than throwaway code. If it does not hold, the fallback is a
FastMCP middleware that stores the token in a context variable the hook reads.

## Out of scope

- The consent UI and connected-apps screen, which live in the main repository.
- Changes to the authorization server; its contract is fixed and documented above.
- Fine-grained scopes. This server requires `mcp` and exposes whatever the route
  file lists; narrowing per tool needs a scope model that does not exist yet.
