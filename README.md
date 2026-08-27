# OpenOps MCP

An [MCP](https://modelcontextprotocol.io) server that turns OpenOps API operations into tools
a language model can call. Depending on what the API offers, that means listing workflows,
inspecting runs, reading block metadata, and building and testing workflows.

There are two consumers:

| Consumer | Transport | Authentication |
| --- | --- | --- |
| The built-in AI chat inside OpenOps | `stdio` | The API spawns the server per chat request with one short-lived token |
| External clients such as Claude Code or Codex | `http` | Every request carries the caller's OAuth token, verified locally and exchanged for an API token |

## How tools get here

The API decides which operations become tools. This server fetches a filtered OpenAPI
document and turns every operation in it into a tool. It holds no allow-list and no list of
paths, and it knows nothing about how any given OpenOps instance is set up.

```
GET {api}/v1/mcp/openapi.json?profile=agent
  → { "paths": { …the operations this profile exposes… },
      "x-openops-mcp": { "multiProject": true } }
      → one tool per operation
```

The API publishes two profiles, named after whoever reads them. `chat` is for the built-in AI
chat. `agent` is for external OAuth clients. What goes in each is up to the API, and the same
profile can mean different things on different deployments: one might answer `agent` with a
workflow-authoring surface and another with a read-only one.

A few things follow from that. The same binary serves whatever the API it points at publishes.
Tool names come from each operation's `operationId` and tool descriptions from its
`description`, both of which live in the API's route definitions, so renaming a tool or
improving what a model is told about it means editing the API. And asking for a profile the
API doesn't recognise stops the process at startup instead of quietly producing half a tool
list.

## Requirements

- Python 3.10–3.13
- [uv](https://docs.astral.sh/uv/)
- A running OpenOps API

## Install

```bash
uv sync                 # creates .venv from uv.lock
```

Keep the virtual environment at `.venv` in the repository root. The API spawns this server as
`<path>/.venv/bin/python <path>/main.py`, so that path is part of the contract.

## Dependencies

`uv.lock` is the source of truth. `requirements.txt` is generated from it, so don't edit it by
hand.

Two things read that format and neither reads a lockfile. Snyk is one, and it's the only
dependency scanning this repository has; before the export existed it reported `1 test was
skipped` and passed every run without looking at anything. The other is the App container
image, which installs from it. Between the migration to uv deleting the file and the export
restoring it, that image couldn't build.

Keeping a generated copy of the lockfile is a compromise, and it only works because the copy
can't drift. CI reruns the same export and fails on any difference, so a dependency change
that skips the regeneration won't merge. One wrinkle: uv writes the invoking command into the
file header, so CI has to use exactly the same command. Different flags or a different output
path change the header and the comparison fails for no real reason.

After changing a dependency:

```bash
uv lock
uv export --format requirements-txt --no-dev --no-emit-project --frozen -o requirements.txt
```

Commit `pyproject.toml`, `uv.lock` and `requirements.txt` together. `--no-dev` keeps
development tools out of what ships and what gets scanned. `--no-emit-project` leaves this
package out, because the container runs it from source rather than installing it, which is
also why `main.py` sits at the repository root.

## Running it

### stdio, driven by the API

There's nothing to configure here. The API spawns the process per chat request and passes the
transport, a filtered document written to a temporary file, a short-lived token and the API
URL. The one setting lives on the API side and points at this checkout:

```bash
OPS_OPENOPS_MCP_SERVER_PATH=/path/to/openops-mcp
```

It defaults to the container path (`/root/.mcp/openops-mcp`). If you run the API outside
Docker without setting it, the spawn fails silently and the chat ends up with no OpenOps
tools.

### stdio, driven by you

Handy for inspecting the tool surface with any MCP client. Pass the settings on the command
line so they take precedence over anything in a local `.env`:

```bash
MCP_TRANSPORT=stdio \
OPENOPS_API_URL=http://localhost:3000 \
OPENOPS_MCP_PROFILE=agent \
AUTH_TOKEN=<bearer token> \
.venv/bin/python main.py
```

A browser session token works as `AUTH_TOKEN`, since the tool routes accept `USER` as well as
`SERVICE` principals:

```bash
curl -s -i -X POST http://localhost:3000/v1/authentication/sign-in \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}' \
  | grep -i '^set-cookie: token=' | sed 's/.*token=\([^;]*\).*/\1/'
```

stdio never offers project selection, whatever the API reports. Its token is minted for one
project and can't move.

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

A healthy start prints three lines:

```
Loaded 22 paths from http://localhost:3000/v1/mcp/openapi.json?profile=agent
Offered project selection on 22 paths
Serving MCP over HTTP on 127.0.0.1:3020
```

No first line means the API is unreachable or OAuth is disabled. No second line means the API
reported a single project, so no tool will take a `project_id`.

Point a client at `http://localhost:3020/mcp` and it will discover the authorization server,
register itself, send the user to the browser to approve, and come back with tools.

Three values have to agree with the API's configuration or nothing authenticates:

| Here | In the API |
| --- | --- |
| `OPENOPS_MCP_ISSUER` | `OPS_OAUTH_ISSUER_URL` |
| `OPENOPS_MCP_RESOURCE_URL` | `OPS_MCP_RESOURCE_URL` |
| `OPENOPS_MCP_CLIENT_SECRET` | `OPS_OAUTH_RS_CLIENT_SECRET` |

The issuer has to match exactly, because it's compared against the `iss` claim on every
inbound token. The resource URL has to differ from the issuer; startup refuses otherwise,
since equal audiences would let a token minted for one resource be accepted by the other.

Both must use `https` unless they point at loopback. The client secret travels to the issuer
as HTTP Basic and the resource URL is this server's advertised identity, so cleartext to a
remote host is refused at startup. `OPENOPS_API_URL` is exempt, because tool calls are
pod-to-pod inside a cluster.

## Docker

The image serves the http transport as its own service next to the API. stdio isn't what it's
for: the API image vendors this repository and spawns it per chat request.

```bash
docker build -t openops-mcp .

docker run --rm -p 3020:3020 \
  -e OPENOPS_API_URL=http://openops-api:3000 \
  -e OPENOPS_MCP_PROFILE=agent \
  -e OPENOPS_MCP_ISSUER=https://example.com/api \
  -e OPENOPS_MCP_RESOURCE_URL=https://example.com/mcp \
  -e OPENOPS_MCP_CLIENT_SECRET=<at least 32 characters> \
  openops-mcp
```

`MCP_TRANSPORT=http`, `MCP_HTTP_HOST=0.0.0.0` and `MCP_HTTP_PORT=3020` are the image's
defaults. The environment comes from `uv.lock` with the same `uv sync --frozen --no-dev` CI
runs; the Dockerfile explains the rest of its choices inline.

To test against an API on your machine, the issuer has to be `localhost`, and inside a
container that's the container. Run with `--network host` (on Docker Desktop, enable it under
Resources → Network) and point `OPENOPS_API_URL` and `OPENOPS_MCP_ISSUER` at
`http://localhost:3000`.

## Configuration reference

| Variable | Transport | Default | Purpose |
| --- | --- | --- | --- |
| `MCP_TRANSPORT` | both | `stdio` | `stdio` or `http` |
| `OPENOPS_API_URL` | both | required | Base URL for tool calls |
| `OPENOPS_MCP_PROFILE` | both | `agent` | `chat` or `agent`; which published surface to serve |
| `AUTH_TOKEN` | stdio | required | Bearer token for every downstream call |
| `OPENOPS_MCP_ISSUER` | http | required | Authorization server, and the expected `iss` claim |
| `OPENOPS_MCP_RESOURCE_URL` | http | required | This server's canonical URI and expected token audience |
| `OPENOPS_MCP_CLIENT_SECRET` | http | required | Credential for token exchange, 32 characters or more |
| `MCP_HTTP_HOST` | http | `0.0.0.0` | Bind address; narrow it for local use |
| `MCP_HTTP_PORT` | http | `3020` | Bind port |
| `OPENOPS_API_OPENAPI_URL` | both | derived | Overrides where the document is fetched from |
| `OPENOPS_API_OPENAPI_PATH` | both | unset | Read the document from a file instead; wins over any URL |
| `LOG_LEVEL` | both | `INFO` | Console log level. `DEBUG` also makes every dependency verbose |
| `LOGZIO_TOKEN`, `ENVIRONMENT` | both | unset | Optional log shipping |

`API_BASE_URL`, `OPENAPI_SCHEMA_URL` and `OPENAPI_SCHEMA_PATH` still work as deprecated
aliases for the three `OPENOPS_API_*` names, and log a warning.

Settings are read and validated once at startup, so a misconfiguration names the variable at
fault and exits rather than turning into a confusing request failure later.

`.env` in the repository root is loaded, but an exported variable always wins: the loader never
overrides what's already in the environment. It does fill in variables the environment leaves
out, which is why the API passes `MCP_TRANSPORT` explicitly when it spawns this server.
Without that, a checkout configured for http would hijack a stdio process.

## Acting in more than one project

When the API reports more than one project and the transport is http, every tool takes an
optional `project_id`:

```
List_Workspaces()                → the projects this user belongs to
List_Flows(project_id="…")       → acts in that project
List_Flows()                     → acts in the project the connection was authorized for
```

The argument never reaches the API. This server strips it and uses it to decide which token to
mint, and the API still takes the project from that token's claim, bounded by the user's own
membership. Naming a project the user doesn't belong to gets refused with `invalid_target`.

Nothing is stored. There's no current project on the server, which is what lets any replica
serve any request. It also means that leaving the argument out goes back to the project the
connection was authorized for, not the one used last. The workspace-listing tool's description
tells the model to keep passing the id it picked.

## Logging

Everything goes to stderr at `INFO`, plus Logz.io when `LOGZIO_TOKEN` is set.

stderr matters here. On the stdio transport stdout carries the MCP protocol, so a single line
written there corrupts the stream and the client drops the session.

`LOG_LEVEL=DEBUG` raises the level on the root logger, which makes every dependency verbose as
well. Useful locally, worth avoiding wherever this server's stderr is collected.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `Cannot start: OPENOPS_MCP_ISSUER is required` | Running http mode with stdio configuration, or with the pre-rename variable names |
| `could not fetch the OpenAPI document from …` | API unreachable, or OAuth disabled so `/v1/mcp/openapi.json` isn't served |
| Every request 401s | `OPENOPS_MCP_ISSUER` doesn't match the API's issuer exactly, or the audience doesn't match `OPS_MCP_RESOURCE_URL` |
| Token exchange refused | `OPENOPS_MCP_CLIENT_SECRET` differs from the API's `OPS_OAUTH_RS_CLIENT_SECRET` |
| No tool takes `project_id` | The API reported `multiProject: false`, or the transport is stdio |
| The built-in chat has no OpenOps tools | `OPS_OPENOPS_MCP_SERVER_PATH` isn't set on the API, so the spawn used the container path |
| A client drops the session with a JSON-RPC parse error | Something wrote to stdout, which is the protocol channel on stdio |
| `address already in use` from a spawned stdio process | A local `.env` set `MCP_TRANSPORT=http`; the spawn has to pass the transport explicitly |

## Development

```bash
.venv/bin/pytest                            # unit plus integration over a real transport
.venv/bin/mypy openops_mcp                  # strict
.venv/bin/ruff check openops_mcp tests
```

The integration tests in `tests/integration/` run a real HTTP transport with a real auth
provider against a mocked API and authorization server, so the authentication path gets
exercised rather than stubbed.

[docs/architecture.md](docs/architecture.md) covers how the pieces fit together and why.
