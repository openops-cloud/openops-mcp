# How the OpenOps MCP server works

This describes the server as it is and the reasoning behind it. [README.md](../README.md)
covers running and configuring it; this covers the internals and the decisions.

## Contents

- [Overview](#overview)
- [Where tools come from](#where-tools-come-from)
- [Authentication](#authentication)
- [Choosing a project](#choosing-a-project)
- [Module map](#module-map)
- [Dependencies](#dependencies)
- [What the server does not do](#what-the-server-does-not-do)
- [Failure modes](#failure-modes)
- [Extending it](#extending-it)

## Overview

Every request goes through the same three steps. Only the middle one differs between the two
transports.

```
1. A model calls a tool
       │
2. The client for this transport authorizes the request
       │   stdio → one static bearer token, set once at startup
       │   http  → the caller's verified token, exchanged for an API-audience token
       │
3. The request reaches the OpenOps API and the response is passed straight back
```

`server.py` is the only place a server gets built. Both transports arrive there with the same
document and the same route mapping, so the tool surface can't differ between them. Only the
client's authentication does.

Two details of that construction are easy to undo by accident:

`validate_output=False`, because the API's response schemas are generated and don't always
mark nullable fields. Validating against them turns a healthy 200 into a tool error like
`None is not of type 'string'` for a field the API legitimately omits. A model copes with a
missing field far better than it copes with losing the whole response.

`stateless_http=True` on the http transport, so each request is authorized by the token it
carries rather than by whichever token opened the session.

## Where tools come from

The API publishes a filtered OpenAPI document per profile. This server fetches it and turns
every operation in it into a tool.

```
config.py     OPENOPS_MCP_PROFILE=agent
                → {api_url}/v1/mcp/openapi.json?profile=agent

openapi.py    fetch_spec()  or  read_spec()   → validate that "paths" is a mapping
              build_route_maps()              → [RouteMap(methods="*", pattern=".*", TOOL)]

server.py     FastMCP.from_openapi(spec, route_maps=…)
```

`build_route_maps()` returns a single catch-all that maps everything to a tool. The document
is already the allow-list, so there's nothing left to filter. It's written out rather than
left to FastMCP's default so the intent survives a version bump.

### Why the API owns the list

This server used to carry its own allow-list in two YAML files naming paths and methods, while
the API kept the same list for its built-in chat. Two sources of truth in two languages, and
they had already drifted.

They also encoded the wrong distinction. The files were named after deployment variants, but
what they described was consumers: the built-in chat and an external agent get different
surfaces from the same API. Those are independent questions, and a file here could only guess
at the second one. Only the API knows which routes it registered.

### Two ways in

Both end at the same validation.

Over HTTP, `fetch_spec` is used by the long-lived http server. The endpoint is public, since it
exposes the shape of the API rather than any data, which matters because the server has no
credential of its own at startup.

From a file, `read_spec` is used when the API spawns this process. The API writes the document
it already computed and passes the path. A process gets spawned per chat request, so a
self-call per spawn would cost more than a write. `OPENOPS_API_OPENAPI_PATH` wins over any URL.

### Names and descriptions

Tool names come from `operationId` and descriptions from `description`, both straight out of
the document. There's no override mechanism here on purpose: the previous one lived in a YAML
file in this repository, far from the routes it renamed, and nobody used it. Copy describing a
tool belongs next to the route that implements it.

FastMCP slugifies the value and truncates at 56 characters, so `'List Flows'` becomes
`List_Flows`. An operation with no `operationId` falls back to `method_path`, giving you
something like `GET_v1project`, which is a useful sign that a route joined a profile without
being named.

## Authentication

### stdio

`auth/static.py` is eighteen lines because there's nothing to decide. The API spawns this
server with a short-lived token belonging to the signed-in user, so one header on one client
covers it. There's no second identity to tell apart, and the process boundary is the security
boundary.

### http

Three concerns, easier to follow separately.

**Verifying the caller.** `auth/oauth.py` builds a `JWTVerifier` against the authorization
server's published keys, so no request costs a round trip:

```python
JWTVerifier(
    jwks_uri=f"{issuer}/v1/oauth/jwks.json",
    issuer=issuer,
    audience=resource_url,     # this server's canonical URI
    required_scopes=["mcp"],
)
```

The audience check stops this server being used to launder a token, since a token minted for
the API carries a different audience and gets refused.

That goes inside `RemoteAuthProvider`, which serves the RFC 9728 protected-resource metadata
and the `WWW-Authenticate` challenge. A client uses them to find out where to authorize after
its first unauthenticated request:

```
POST /mcp  (no credential)
  → 401  www-authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp"
    → {"resource": "…/mcp", "authorization_servers": ["…"], "scopes_supported": ["mcp"]}
      → the client discovers, registers, and sends the user to approve
```

`base_url` gets this server's origin rather than its resource identifier, because FastMCP
appends the transport's mount path to derive both the resource and the metadata location.
Passing the full resource URI would double that path segment and point clients at metadata
that doesn't exist. For the same reason an ingress must not rewrite the mount path: a stripped
prefix makes the advertised resource and the actual audience diverge.

`OPENOPS_MCP_ISSUER` and `OPENOPS_MCP_RESOURCE_URL` have to be `https` unless they name
loopback. The first receives the client secret as HTTP Basic, and the second is the identity
this server advertises. `OPENOPS_API_URL` is exempt, since tool calls stay inside the cluster
and only the OAuth endpoints are public.

**Exchanging it.** The client's token is addressed to this server rather than the API, so it's
never forwarded, following the MCP authorization spec's no-token-passthrough rule.
`auth/exchange.py` presents it to the authorization server in an RFC 8693 exchange and gets
back a separate API-audience token:

```
POST {issuer}/v1/oauth/token
Authorization: Basic base64(openops-mcp-rs:{client_secret})
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token={the caller's token}
project_id={optional}
```

The client id is fixed at `openops-mcp-rs` and has to match `RS_CLIENT_ID` in the API.

**Presenting it.** `_AuthorizingTransport` in `auth/oauth.py` authorizes each outbound request.
It's a transport rather than an httpx event hook because a hook can't retry, and one retry is
what turns a revoked connection into a message worth reading:

```
handle_async_request
  ├─ read the caller's token from the request being served
  ├─ pop the project_id header, if present
  ├─ exchange, set Authorization, send
  └─ on 401: evict the cached token, then if the body can be replayed, exchange and send once more
       └─ still 401 → evict again; a fresh token refused is worth no more than the one it replaced
```

The exchange fails closed. If it fails, the tool call fails, because letting the request
continue unauthenticated would surface as a confusing 401 from the API and hide the cause.

The caller's token is read from the request headers rather than FastMCP's
`get_access_token()`, which returns `None` during tool execution in FastMCP 3.4.5: the auth
context doesn't reach the task the tool runs in, while the HTTP request context does. That
isn't a way around verification. A request only reaches a tool after the provider has checked
signature, issuer, audience and scopes, and the authorization server verifies the subject token
again during the exchange.

### Caching

Exchanged tokens are cached, keyed on `(sha256(caller token), project)`.

Keying on the token rather than the user matters because two tokens for one user may name
different projects and mustn't share an entry. Keying per project means a switch is never
served a token minted for somewhere else.

Lifetime is `min(900s, reported expiry − 5s)`. The margin keeps a token from being used in the
instant between the check and the request.

Concurrent misses collapse into one in-flight exchange, shielded so that one caller abandoning
its request can't cancel the exchange others are waiting on. An agent calls its tools in
bursts, so the misses that matter arrive together.

`evict(token)` drops every project held for that caller, because a 401 means the caller's own
credential is no longer accepted whichever project it was acting in. A size sweep reclaims
expired entries at 10,000 entries and clears the cache rather than growing past it, since
caller tokens rotate and every entry eventually becomes garbage.

A window this long is acceptable because reuse doesn't delay revocation. The API re-checks the
grant, the user's status and their project membership on every request it serves, so a cached
token stops being accepted the moment a connection is revoked.

## Choosing a project

When the API's document carries `x-openops-mcp: {"multiProject": true}` and the transport is
http, the server gives every operation an optional `project_id`:

```python
inject project_id  ⟺  is_multi_project(spec)  and  transport == "http"
```

An absent or malformed capability means no. Switching is the permissive answer, so a document
that doesn't ask for it never gets it. stdio never gets it either, because its token is minted
for one project per chat request and can't move.

### Why a header

Tools are generated from the document, so declaring the argument there is the only way to give
one a new parameter. Of the four OpenAPI parameter locations, `header` is the one that reaches
the tool's input schema and can also be removed with a single `pop` before the request leaves
the process. The name is created and destroyed inside one process and never crosses a proxy.

```
① model      List_Flows(limit=10, project_id="p1")
② FastMCP    GET /v1/flows/?limit=10   headers: {project_id: "p1"}   ← not sent yet
③ transport  pop the header — now gone — and exchange with project_id=p1
④ API        mints a token whose project_id claim IS p1
⑤ transport  sends the original request with that token, no project_id header
⑥ API        reads the project from the claim, re-checks membership
```

A stray `project_id` header would be ignored by the API rather than rejected, since it reads
only body and query for a project. A leak would act in the wrong project without failing,
which is why the strip has a dedicated integration test rather than a comment.

If an operation already declares a `project_id` parameter, startup fails and names the
operation. FastMCP would otherwise rename ours to `project_id__header` and hand the model an
argument nobody chose.

### Why nothing is stored

There's no current-project state on this server, and the concurrency boundary is why. A
connection is authorized per machine, but conversations run per terminal, and two terminals on
one machine share a connection. Stored state would let one terminal silently move the other,
and a wrong-project write is worse than the failure it would replace.

That has a real cost. Leaving the argument out acts in the project the connection was
authorized for, not the one used last, so a model that stops passing it drifts home quietly.
The mitigation lives in the workspace-listing tool's description, which tells the model to keep
passing the id it chose. The per-tool argument description stays one sentence, because it
repeats in every tool's schema.

What this buys is that any replica can serve any request. Nothing needs sticky routing, a
restart costs at most one exchange, and two users' requests share nothing.

### What counts as state

The process holds two caches, for exchanged tokens and JWKS, plus an in-flight task map. All
three are derived: flush them and every answer is identical, only slower. The question to ask
of anything new is whether flushing it would change an outcome. For the caches it wouldn't. For
a stored current project it would, which is why there isn't one.

## Module map

| Module | Responsibility |
| --- | --- |
| `main.py` | Root entrypoint. The API spawns `<path>/.venv/bin/python <path>/main.py`, so this path is a contract |
| `__main__.py` | Read configuration, load the document, build for the transport, serve |
| `config.py` | Environment into validated settings, all checked once at startup |
| `openapi.py` | Read the document, read the capability, inject `project_id`, map routes to tools |
| `server.py` | The single place a `FastMCP` is constructed |
| `auth/static.py` | stdio: one bearer token for the life of the process |
| `auth/oauth.py` | http: verify the caller, exchange, present, retry |
| `auth/exchange.py` | RFC 8693 exchange with caching, coalescing and eviction |
| `logging_config.py` | stderr console logging, optional Logz.io shipping |

`auth/oauth.py` is imported lazily inside `build()`, so the stdio path doesn't pay for OAuth
machinery it never uses.

## Dependencies

`uv.lock` is authoritative. `requirements.txt` is generated from it, and the generation is
verified in CI rather than trusted.

It exists because two consumers read that format and neither reads a lockfile: Snyk, the only
dependency scanning this repository has, and the App container image, which pip-installs it.
Snyk skipped the Python manifest entirely until the export appeared, passing every run while
examining nothing, and the image couldn't build between the migration to uv and the export
being restored.

This is the one place in the repository where the same information lives in two files, which
sits awkwardly next to the tool-surface work that deleted exactly that kind of duplication. The
difference is that this copy is generated and machine-checked. CI re-runs the identical export
and fails on any difference, so it can't say anything the lockfile doesn't. A hand-maintained
second list would have no such guarantee.

The container could avoid the file, since `uv sync --frozen` produces the same environment and
the image already installs uv, but Snyk can't. So the file stays and the image may as well use
it. If Snyk gains lockfile support, the export and its CI job can both go.

## What the server does not do

- Know anything about the deployment. Nothing in the package describes how a particular
  OpenOps instance is configured; the document it's served decides everything.
- Keep an allow-list. That went with the YAML files it lived in.
- Override tool names or descriptions. Those belong in the API's route definitions.
- Store a current project. See above.
- Use a database or Redis. The only state is derived caches.

## Failure modes

It helps to know which failures are loud and which are quiet.

Loud at startup: a missing or malformed variable names itself and exits 1. An unreachable API,
a document with no `paths`, an unknown profile, an issuer equal to the resource URL, a client
secret under 32 characters, an operation that already declares `project_id`. All of them stop
the process with one clear line rather than a traceback.

Loud per request: a token that fails verification never reaches a tool, a failed exchange fails
the tool call, and a project the user doesn't belong to is refused with `invalid_target`.

Quiet, and therefore tested: the `project_id` strip. If it regressed the API would ignore the
header and act in the token's project, which is a wrong answer rather than an error.

Quiet, and worth remembering: writing anything to stdout on the stdio transport corrupts the
protocol stream, and the client reports a JSON-RPC parse error that points nowhere near the log
line responsible. That's why all logging goes to stderr, at `INFO` unless `LOG_LEVEL` says
otherwise. `DEBUG` raises the root logger, so every dependency joins in.

## Extending it

To expose a new API operation as a tool, add it to the profile in the API. Nothing changes
here. Give it a clean `operationId` and a description written for a model rather than for a
reference page, since those become the tool's name and its documentation.

To add a profile, define it in the API. This server validates the name against a local set in
`config.py`, which has to learn the new one. That's the last place knowledge is duplicated
across the two repositories.

To change what a tool argument tells the model, edit the API's route description. The per-tool
`project_id` description lives in `openapi.py`, and the workflow guidance that would otherwise
bloat it lives on the workspace-listing route in the API.

Before trusting a change to the stdio path, spawn the process the way the API does rather than
building the server in-process. The stdout logging defect passed every in-process check and
failed immediately under a real subprocess, because only a subprocess has a stdout that
matters.
