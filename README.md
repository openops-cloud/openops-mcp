# OpenOps MCP

An [MCP](https://modelcontextprotocol.io) server that exposes OpenOps API operations as
tools, so a language model can list workflows, inspect runs, read block metadata and — on an
enterprise deployment — author and test workflows.

It serves two very different consumers from one codebase:

| Consumer | Transport | How it is authenticated |
| --- | --- | --- |
| The **built-in AI chat** inside OpenOps | `stdio` | The API spawns this server per chat request and hands it one short-lived token |
| **External clients** — Claude Code, Codex | `http` | Each request carries the caller's own OAuth token, verified locally and exchanged for an API token |

## The one idea worth understanding first

**This server does not decide which operations become tools. The API does.**

It fetches a filtered OpenAPI document from the API and turns every operation in it into a
tool. There is no allow-list here, no list of paths, and no notion of edition — grep the
package for "enterprise" and you get nothing. If a tool exists, it is because the API
published it.

```
GET {api}/v1/mcp/openapi.json?profile=agent
  → { "paths": { …the operations this profile exposes… },
      "x-openops-mcp": { "multiProject": true } }
      → one tool per operation
```

The API publishes two **profiles**, named after consumers rather than editions:

- **`chat`** — what the built-in AI chat gets. Read-mostly: it reasons about workflows that
  exist rather than authoring them.
- **`agent`** — what external OAuth clients get. On an enterprise deployment this is the
  richer `/mcp/*` authoring surface; on a community deployment it is the same read surface
  as `chat`.

Consequences worth internalising:

- The same binary, unchanged, gives an enterprise or a community tool surface depending only
  on which API it points at.
- Tool **names** come from each operation's `operationId`, and tool **descriptions** from its
  `description`. Both live in the API's route definitions, next to the code they describe. To
  rename a tool, or improve what a model is told about it, edit the API.
- A profile the API does not recognise fails at startup rather than producing a partial tool
  list.

## Requirements

- Python 3.10–3.13
- [uv](https://docs.astral.sh/uv/) — `uv.lock` is the source of truth for dependencies
- A running OpenOps API

## Install

```bash
uv sync                 # creates .venv from uv.lock
```

The virtual environment must live at `.venv` in the repository root: the API spawns this
server as `<path>/.venv/bin/python <path>/main.py`, and that path is part of the contract.

## Running it

### stdio, driven by the API

Nothing to configure here. The API spawns the process per chat request and passes the
transport, a filtered document written to a temporary file, a short-lived token, and the API
URL. The only setting is on the API side, pointing at this checkout:

```bash
OPS_OPENOPS_MCP_SERVER_PATH=/path/to/openops-mcp
```

Its default is the container path (`/root/.mcp/openops-mcp`), so **running the API outside
Docker without this variable makes the spawn fail silently** and the chat quietly has no
OpenOps tools.

### stdio, driven by you

Useful for inspecting the tool surface with any MCP client. Pass settings on the command line
so they win over anything in a local `.env`:

```bash
MCP_TRANSPORT=stdio \
OPENOPS_API_URL=http://localhost:3000 \
OPENOPS_MCP_PROFILE=agent \
AUTH_TOKEN=<bearer token> \
.venv/bin/python main.py
```

A browser session token works for `AUTH_TOKEN` — the tool routes accept both `USER` and
`SERVICE` principals:

```bash
curl -s -i -X POST http://localhost:3000/v1/authentication/sign-in \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}' \
  | grep -i '^set-cookie: token=' | sed 's/.*token=\([^;]*\).*/\1/'
```

**stdio never offers project selection**, whatever the API reports: that token is minted for
one project and cannot move.

### http, for external clients

```bash
MCP_TRANSPORT=http
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=3020

OPENOPS_API_URL=http://localhost:3000
OPENOPS_MCP_PROFILE=agent

OPENOPS_MCP_ISSUER=http://localhost:3000
OPENOPS_MCP_RESOURCE_URL=http://localhost:3020/mcp
OPENOPS_MCP_CLIENT_SECRET=<at least 32 characters>
```

```bash
.venv/bin/python main.py
```

Three lines confirm a healthy start, and each says something:

```
Loaded 22 paths from http://localhost:3000/v1/mcp/openapi.json?profile=agent
Offered project selection on 22 paths
Serving MCP over HTTP on 127.0.0.1:3020
```

If the first is missing, the API is unreachable or OAuth is disabled. If the second is
missing, the API reported a single project, so no tool takes a `project_id`.

Then point a client at `http://localhost:3020/mcp`. It discovers the authorization server,
registers itself, sends the user to the browser to approve, and comes back with tools.

**Three values must agree with the API's configuration**, or nothing authenticates:

| Here | In the API |
| --- | --- |
| `OPENOPS_MCP_ISSUER` | `OPS_OAUTH_ISSUER_URL` |
| `OPENOPS_MCP_RESOURCE_URL` | `OPS_MCP_RESOURCE_URL` |
| `OPENOPS_MCP_CLIENT_SECRET` | `OPS_OAUTH_RS_CLIENT_SECRET` |

The issuer must match **exactly** — it is compared against the `iss` claim on every inbound
token. The resource URL must differ from the issuer; startup refuses otherwise, because equal
audiences would let a token minted for one resource be accepted by the other.

## Configuration reference

| Variable | Transport | Default | Purpose |
| --- | --- | --- | --- |
| `MCP_TRANSPORT` | both | `stdio` | `stdio` or `http` |
| `OPENOPS_API_URL` | both | required | Base URL for tool calls |
| `OPENOPS_MCP_PROFILE` | both | `agent` | `chat` or `agent` — which published surface to serve |
| `AUTH_TOKEN` | stdio | required | Bearer token for every downstream call |
| `OPENOPS_MCP_ISSUER` | http | required | Authorization server, and the expected `iss` claim |
| `OPENOPS_MCP_RESOURCE_URL` | http | required | This server's canonical URI and expected token audience |
| `OPENOPS_MCP_CLIENT_SECRET` | http | required | Credential for token exchange; ≥32 characters |
| `MCP_HTTP_HOST` | http | `0.0.0.0` | Bind address — narrow it for local use |
| `MCP_HTTP_PORT` | http | `3020` | Bind port |
| `OPENOPS_API_OPENAPI_URL` | both | derived | Overrides where the document is fetched from |
| `OPENOPS_API_OPENAPI_PATH` | both | unset | Read the document from a file instead; wins over any URL |
| `LOGZIO_TOKEN`, `ENVIRONMENT` | both | unset | Optional log shipping |

`API_BASE_URL`, `OPENAPI_SCHEMA_URL` and `OPENAPI_SCHEMA_PATH` are accepted as deprecated
aliases for the three `OPENOPS_API_*` names, and log a warning.

Settings are read and validated once, at startup: a misconfiguration names the variable at
fault and exits, rather than surfacing later as a puzzling request failure.

`.env` in the repository root is loaded, but **an exported variable always wins** — the loader
never overrides what is already in the environment. It does fill in variables the environment
*omits*, which is why the API passes `MCP_TRANSPORT` explicitly when it spawns this server:
otherwise a checkout configured for http would hijack a stdio process.

## Acting in more than one project

On an enterprise deployment over http, every tool takes an optional `project_id`:

```
List_Workspaces()                → the projects this user belongs to
List_Flows(project_id="…")       → acts in that project
List_Flows()                     → acts in the project the connection was authorized for
```

The argument never reaches the API. This server strips it and uses it to decide which token
to mint; the API still takes the project from that token's claim, bounded by the user's own
membership. Naming a project the user does not belong to is refused with `invalid_target`.

Nothing is stored. There is no "current project" on the server — which is what lets any
replica serve any request, and means **omitting the argument returns to the project the
connection was authorized for, not the one last used**. The workspace-listing tool's
description tells the model to keep passing the id it chose.

## Logging

Everything goes to **stderr**, at DEBUG, plus Logz.io at INFO when `LOGZIO_TOKEN` is set.

stderr rather than stdout is not a style choice: on the stdio transport stdout carries the MCP
protocol, so a single line written there corrupts the stream and the client drops the session.
The root logger is set to DEBUG, so third-party libraries are verbose too — worth narrowing
if these logs are shipped anywhere.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Cannot start: OPENOPS_MCP_ISSUER is required` | Running http mode with stdio configuration, or with pre-rename variable names |
| `could not fetch the OpenAPI document from …` | API unreachable, or OAuth disabled so `/v1/mcp/openapi.json` is not served |
| Every request 401s | `OPENOPS_MCP_ISSUER` does not match the API's issuer exactly, or the audience does not match `OPS_MCP_RESOURCE_URL` |
| Token exchange refused | `OPENOPS_MCP_CLIENT_SECRET` differs from the API's `OPS_OAUTH_RS_CLIENT_SECRET` |
| No tool takes `project_id` | The API reported `multiProject: false`, or the transport is stdio |
| The built-in chat has no OpenOps tools | `OPS_OPENOPS_MCP_SERVER_PATH` unset on the API, so the spawn used the container path |
| A client drops the session with a JSON-RPC parse error | Something wrote to stdout, which is the protocol channel on stdio |
| `address already in use` from a spawned stdio process | A local `.env` set `MCP_TRANSPORT=http`; the spawn must pass the transport explicitly |

## Development

```bash
.venv/bin/pytest                            # unit plus integration over a real transport
.venv/bin/mypy openops_mcp                  # strict
.venv/bin/ruff check openops_mcp tests
```

Integration tests in `tests/integration/` run a real HTTP transport with a real auth provider
against a mocked API and authorization server, so the authentication path is exercised rather
than stubbed.

See [docs/architecture.md](docs/architecture.md) for how the pieces fit together and why.
