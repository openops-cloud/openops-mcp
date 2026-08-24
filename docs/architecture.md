# How the OpenOps MCP server works

This describes the server as it is, and why it is shaped that way. [README.md](../README.md)
covers running and configuring it; this covers the internals, the reasoning behind the
design decisions, and the failure modes that follow from them.

## Contents

- [The shape of the thing](#the-shape-of-the-thing)
- [Where tools come from](#where-tools-come-from)
- [Authentication](#authentication)
- [Choosing a project](#choosing-a-project)
- [Module map](#module-map)
- [Dependencies](#dependencies)
- [What is deliberately absent](#what-is-deliberately-absent)
- [Failure modes](#failure-modes)
- [Extending it](#extending-it)

## The shape of the thing

Every request follows the same three steps, and the only thing that differs between the two
transports is step 2.

```
1. A model calls a tool
       │
2. The client for this transport authorizes the request
       │   stdio → one static bearer token, set once at startup
       │   http  → the caller's verified token, exchanged for an API-audience token
       │
3. The request reaches the OpenOps API and the response is passed straight back
```

`server.py` is deliberately the only place a server is constructed. Both transports arrive
there with the same document and the same route mapping, so **the tool surface cannot differ
between them** — only the client's authentication does. That is what makes it safe to reason
about the two modes as one system.

Two smaller decisions in that construction are worth knowing:

- **`validate_output=False`.** The API's response schemas are generated and do not always
  mark nullable fields, so validating against them turns a healthy 200 into a tool error —
  `None is not of type 'string'` for a field the API legitimately omits. The consumer is a
  model, which handles a missing field far better than it handles losing the whole response.
- **`stateless_http=True`** on the http transport. Each request is independent, so a request
  is authorized by the token it carries rather than by whichever token opened the session.

## Where tools come from

The API publishes a filtered OpenAPI document per **profile**. This server fetches it and
turns every operation in it into a tool.

```
config.py     OPENOPS_MCP_PROFILE=agent
                → {api_url}/v1/mcp/openapi.json?profile=agent

openapi.py    fetch_spec()  or  read_spec()   → validate that "paths" exists
              build_route_maps()              → [RouteMap(methods="*", pattern=".*", TOOL)]

server.py     FastMCP.from_openapi(spec, route_maps=…)
```

`build_route_maps()` returns a single catch-all mapping everything to a tool. That is not
laziness — the document *is* the allow-list, so there is nothing left to filter. It is stated
explicitly rather than relying on FastMCP's default so the intent survives a version bump.

### Why the API owns the list

This server used to carry its own allow-list: two YAML files naming paths and methods. The
API had the same list for its built-in chat. They were two sources of truth in two languages,
and they had already drifted.

Worse, they encoded the wrong distinction. They were named after deployment variants, but
what they actually described was *consumers*: the built-in chat and an external agent are
given different surfaces by the same API. Those are independent axes — which consumer is
asking, and what that particular deployment chooses to offer it — and a file here could only
ever guess at the second.

Only the API knows which routes it registered, so only the API can answer this. Moving the
list there deleted the duplication and the guessing at once.

### Two ways in

Both reach the same validation:

- **Over HTTP** (`fetch_spec`) for the long-lived http server. The endpoint is public — it
  exposes the shape of the API, not data — so no credential is needed at startup, which
  matters because the server has none of its own at that point.
- **From a file** (`read_spec`) when the API spawns this process. The API writes the
  document it already computed and passes the path. A process is spawned per chat request, so
  a self-call per spawn would cost more than a write. `OPENOPS_API_OPENAPI_PATH` takes
  precedence over any URL.

### Names and descriptions

Tool names come from `operationId`, descriptions from `description`, both taken straight from
the document. There is no override mechanism here, deliberately: the previous one lived in a
YAML file in this repository, far from the routes it renamed, and went unused. Product copy
for a tool belongs next to the route that implements it.

FastMCP slugifies the value and truncates at 56 characters, so `'List Flows'` becomes
`List_Flows`. An operation with no `operationId` falls back to `method_path` —
`GET_v1project` — which is a good signal that a route joined a profile without being named.

## Authentication

### stdio

`auth/static.py`, and it is eighteen lines because there is nothing to decide. The API spawns
this server with a short-lived token belonging to the signed-in user, so one header on one
client is the whole story. There is no second identity to distinguish, and the process
boundary is the security boundary.

### http

Three separate concerns, which are easier to hold separately than together:

**1. Verifying the caller.** `auth/oauth.py` builds a `JWTVerifier` against the authorization
server's published keys, so no request costs a round trip to it:

```python
JWTVerifier(
    jwks_uri=f"{issuer}/v1/oauth/jwks.json",
    issuer=issuer,
    audience=resource_url,     # this server's canonical URI
    required_scopes=["mcp"],
)
```

The audience check is what stops this server being used to launder a token: a token minted
for the API carries a different audience and is refused here.

Wrapped in `RemoteAuthProvider`, which serves the RFC 9728 protected-resource metadata and
the `WWW-Authenticate` challenge — that is how a client discovers where to authorize after
its first unauthenticated request:

```
POST /mcp  (no credential)
  → 401  www-authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp"
    → {"resource": "…/mcp", "authorization_servers": ["…"], "scopes_supported": ["mcp"]}
      → the client discovers, registers, and sends the user to approve
```

`base_url` is passed as this server's *origin*, not its resource identifier: FastMCP appends
the transport's mount path to derive both the resource and the metadata location. Passing the
full resource URI would double that path segment and point clients at metadata that does not
exist. This is also why **an ingress must not rewrite the mount path** — a stripped prefix
makes the advertised resource and the actual audience diverge.

`OPENOPS_MCP_ISSUER` and `OPENOPS_MCP_RESOURCE_URL` must be `https` unless they name loopback:
the first receives the client secret as HTTP Basic, and the second is the identity this server
advertises. `OPENOPS_API_URL` is deliberately exempt, because tool calls stay inside the
cluster while only the OAuth endpoints are public.

**2. Exchanging it.** The client's token is addressed to this server, not to the API, so it is
never forwarded — the MCP authorization spec's no-token-passthrough rule. `auth/exchange.py`
presents it to the authorization server in an RFC 8693 exchange and receives a separate
API-audience token:

```
POST {issuer}/v1/oauth/token
Authorization: Basic base64(openops-mcp-rs:{client_secret})
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token={the caller's token}
project_id={optional}
```

The client id is fixed at `openops-mcp-rs` and must match `RS_CLIENT_ID` in the API.

**3. Presenting it.** `_AuthorizingTransport` in `auth/oauth.py` authorizes each outbound
request. A transport rather than an httpx event hook, because a hook cannot retry — and one
retry is what turns a revoked connection into a message worth reading:

```
handle_async_request
  ├─ read the caller's token from the request being served
  ├─ pop the project_id header, if present
  ├─ exchange, set Authorization, send
  └─ on 401: evict the cached token, then if the body can be replayed, exchange and send once more
       └─ still 401 → evict again; a fresh token refused is worth no more than the one it replaced
```

The exchange is **fail-closed**: if it fails, the tool call fails. Letting the request continue
unauthenticated would surface as a confusing 401 from the API and hide the real cause.

The caller's token is read from the request headers rather than from FastMCP's
`get_access_token()`, which returns `None` during tool execution in FastMCP 3.4.5 — the auth
context does not reach the task the tool runs in, while the HTTP request context does. This is
not a way around verification: a request only reaches a tool after the provider has checked
signature, issuer, audience and scopes, and the authorization server verifies the subject
token again during the exchange.

### Caching, and why it is safe

Exchanged tokens are cached, keyed on `(sha256(caller token), project)`:

- **Keyed on the token, not the user**, because two tokens for one user may name different
  projects and must not share an entry.
- **Keyed per project**, so a switch is never served a token minted for somewhere else.
- **Lifetime** is `min(900s, reported expiry − 5s)`. The margin means a token is never used in
  the instant between the check and the request.
- **Concurrent misses are coalesced** into one in-flight exchange, shielded so one caller
  abandoning its request cannot cancel the exchange others are waiting on. An agent calls its
  tools in bursts, so the misses that matter arrive together.
- **`evict(token)` drops every project** held for that caller. A 401 means the caller's own
  credential is no longer accepted, which is true whichever project it was acting in.
- **A size sweep** reclaims expired entries at 10,000 and clears the cache rather than growing
  past it. Caller tokens rotate, so every entry eventually becomes garbage.

Reuse does not delay revocation, which is what makes a window this long acceptable: the API
re-checks the grant, the user's status and their project membership on **every request it
serves**, so a cached token stops being accepted the moment a connection is revoked.

## Choosing a project

When the API's document carries `x-openops-mcp: {"multiProject": true}` and the transport is
http, the server gives every operation an optional `project_id`:

```python
inject project_id  ⟺  is_multi_project(spec)  and  transport == "http"
```

Absent or malformed means no. Switching is the permissive answer, so it is never the default
for a document that does not ask for it. stdio never gets it: that token is minted for one
project per chat request and cannot move.

### Why a header

Tools are generated from the document, so the only way to give one a new argument is to
declare it there. Of the four OpenAPI parameter locations, `header` is the one that both
reaches the tool's input schema *and* can be removed from the request with a single `pop`
before it leaves the process. The name is created and destroyed inside one process and never
crosses a proxy.

```
① model      List_Flows(limit=10, project_id="p1")
② FastMCP    GET /v1/flows/?limit=10   headers: {project_id: "p1"}   ← not sent yet
③ transport  pop the header — now gone — and exchange with project_id=p1
④ API        mints a token whose project_id claim IS p1
⑤ transport  sends the original request with that token, no project_id header
⑥ API        reads the project from the claim, re-checks membership
```

The API would silently *ignore* a stray `project_id` header rather than reject it — it reads
only body and query for a project — so a leak would act in the wrong project without failing.
That is why the strip has a dedicated integration test rather than a comment.

If an operation already declares a `project_id` parameter, startup fails naming the operation.
FastMCP would otherwise rename ours to `project_id__header` and hand the model an argument
nobody chose.

### Why nothing is stored

There is no current-project state on this server. The concurrency boundary is the reason: a
connection is authorized per machine, but conversations run per terminal, and two terminals on
one machine share a connection. Stored state would let one terminal silently move the other —
and a wrong-project *write* is worse than the failure it would replace.

The cost is real and worth stating plainly: **omitting the argument acts in the project the
connection was authorized for, not the one last used.** A model that stops passing it drifts
home silently. The mitigation is in the workspace-listing tool's description, which tells the
model to keep passing the id it chose; the per-tool argument description stays one sentence,
because it repeats in every tool's schema.

The property this buys is that any replica can serve any request. Nothing needs sticky
routing, a restart costs at most one exchange, and two users' requests share nothing.

### What is state, and what is not

The process holds two caches — exchanged tokens and JWKS — plus an in-flight task map. All
three are derived: flush them and every answer is identical, only slower. The useful test is
"would flushing this change an outcome?" For the caches, no. For a stored current project it
would, which is why there isn't one.

## Module map

| Module | Responsibility |
| --- | --- |
| `main.py` | Root entrypoint. The API spawns `<path>/.venv/bin/python <path>/main.py`, so this path is a contract |
| `__main__.py` | Read configuration, load the document, build for the transport, serve |
| `config.py` | Environment into validated settings. Everything checked once, at startup |
| `openapi.py` | Read the document, read the capability, inject `project_id`, map routes to tools |
| `server.py` | The single place a `FastMCP` is constructed |
| `auth/static.py` | stdio: one bearer token for the life of the process |
| `auth/oauth.py` | http: verify the caller, exchange, present, retry |
| `auth/exchange.py` | RFC 8693 exchange with caching, coalescing and eviction |
| `logging_config.py` | stderr console logging, optional Logz.io shipping |

`auth/oauth.py` is imported lazily inside `build()`, so the stdio path does not pay for the
OAuth machinery it never uses.

## What is deliberately absent

- **No knowledge of the deployment.** Nothing in the package describes how any particular
  OpenOps instance is configured. The document it is served decides everything.
- **No allow-list.** Deleted along with the YAML files it lived in.
- **No tool name or description overrides.** They belong in the API's route definitions.
- **No stored project.** See above.
- **No database or Redis.** The only state is derived caches.

## Dependencies

`uv.lock` is authoritative. `requirements.txt` is generated from it, and the generation is
verified in CI rather than trusted.

It exists because two consumers read that format and neither reads a lockfile: Snyk, which is
the only dependency scanning this repository has, and the App container image, which
pip-installs it. Snyk skipped the Python manifest entirely until the export appeared — a check
that passed on every run while examining nothing — and the image could not build between the
migration to uv and the export being restored.

This is the one place in the repository where the same information lives in two files, which
is worth being uncomfortable about, since a duplicated allow-list is exactly what the tool
surface work deleted. The difference is that this copy is generated and machine-checked: CI
re-runs the identical export and fails on any difference, so it cannot say something the
lockfile does not. A hand-maintained second list would have no such guarantee.

The container could avoid the file — `uv sync --frozen` produces the same environment, and the
image already installs uv — but Snyk cannot, so the file stays and the image may as well use
it. If Snyk gains lockfile support, the export and its CI job can both go.

## Failure modes

Worth knowing which failures are loud and which are quiet.

**Loud, at startup:** a missing or malformed variable names itself and exits 1. An unreachable
API, a document with no `paths`, an unknown profile, an issuer equal to the resource URL, a
client secret under 32 characters, an operation that already declares `project_id` — all stop
the process with one clear line rather than a traceback.

**Loud, per request:** a token that fails verification never reaches a tool. A failed exchange
fails the tool call. A project the user does not belong to is refused `invalid_target`.

**Quiet, and therefore tested:** the `project_id` strip. If it regressed, the API would ignore
the header and act in the token's project — a wrong answer, not an error.

**Quiet, and worth remembering:** writing anything to stdout on the stdio transport corrupts
the protocol stream, and the client reports a JSON-RPC parse error rather than pointing at the
log line responsible. All logging goes to stderr for this reason, at `INFO` unless `LOG_LEVEL`
says otherwise — `DEBUG` raises the root logger, so every dependency joins in.

## Extending it

**To expose a new API operation as a tool:** add it to the profile in the API. Nothing changes
here. Give it a clean `operationId` and a description written for a model rather than for a
reference page — those become the tool's name and its documentation.

**To add a profile:** define it in the API. This server validates the name against a local set
in `config.py`, which must learn the new one — the one remaining place where knowledge is
duplicated across the two repositories.

**To change what a tool argument tells the model:** edit the API's route description. The
per-tool `project_id` description lives in `openapi.py`, and the workflow guidance that would
otherwise bloat it lives on the workspace-listing route in the API.

**Before trusting a change to the stdio path:** spawn the process the way the API does, rather
than building the server in-process. The stdout logging defect passed every in-process check
and failed immediately under a real subprocess, because only a subprocess has a stdout that
matters.
