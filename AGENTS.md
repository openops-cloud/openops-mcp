# Agent Onboarding — openops-cloud/openops-mcp

An MCP server exposing OpenOps API operations as tools. Read [README.md](./README.md) to run
it and [docs/architecture.md](./docs/architecture.md) for how it works and why. This file is
what to know before changing it.

Work from the repository root. Prefer small, scoped changes. Avoid unrelated refactors.

---

## The rule that shapes everything

**This server does not decide which operations become tools. The API does.**

It fetches a filtered OpenAPI document from the OpenOps API and turns every operation in it
into a tool. There is no allow-list, no list of paths, and no notion of edition anywhere in
the package — `grep -ri enterprise openops_mcp/` returns nothing, and that is a property to
preserve, not an accident.

So before adding anything here, ask where it belongs:

| You want to… | Change it in |
| --- | --- |
| Expose a new operation as a tool | The **API**, in the profile registry |
| Rename a tool | The **API** — names come from `operationId` |
| Change what a model is told about a tool | The **API** — descriptions come from the route |
| Restrict which operations a consumer sees | The **API**, by profile |
| Change transports, authentication, or caching | Here |

A change that adds a path, a method, or a tool name to this repository is almost certainly in
the wrong repository.

## Repo structure

| Path | Responsibility |
| --- | --- |
| `main.py` | Root entrypoint. The API spawns `<path>/.venv/bin/python <path>/main.py` — **this path is a contract**, do not move it |
| `openops_mcp/__main__.py` | Read configuration, load the document, build for the transport, serve |
| `openops_mcp/config.py` | Environment into validated settings, all checked once at startup |
| `openops_mcp/openapi.py` | Read the document, read its capability block, inject `project_id`, map routes to tools |
| `openops_mcp/server.py` | The single place a `FastMCP` is constructed |
| `openops_mcp/auth/static.py` | stdio: one bearer token for the life of the process |
| `openops_mcp/auth/oauth.py` | http: verify the caller, exchange, present, retry |
| `openops_mcp/auth/exchange.py` | RFC 8693 exchange with caching, coalescing and eviction |
| `openops_mcp/logging_config.py` | stderr logging, optional Logz.io shipping |
| `tests/unit/` | Fast, isolated |
| `tests/integration/` | A real HTTP transport with a real auth provider against a mocked API |

## Setup

```bash
uv sync --extra dev
```

Python 3.10–3.13. The virtual environment must be `.venv` in the repository root, because the
spawn contract names that path.

## Commands

```bash
uv run pytest                              # whole suite
uv run pytest tests/unit/test_config.py    # one file
uv run mypy openops_mcp                    # strict; no new ignores without a reason
uv run ruff check openops_mcp tests
```

Run all three before proposing a change. CI runs exactly these, plus the suite on both 3.10
and 3.13, plus a check that `requirements.txt` still matches the lockfile.

## Testing expectations

Write the test first, and **watch it fail for the reason you expect**. Two real examples from
this repository where a passing test was hiding a bug:

- A test asserting an empty `project_id` was not sent passed against the broken code, because
  `parse_qsl` drops blank values by default. It needed `keep_blank_values=True`.
- Every in-process test passed while the server was unusable over stdio, because logging went
  to stdout and only a real subprocess has a stdout that matters.

So: **when changing anything on the stdio path, spawn the process.** Building the server
in-process is not a substitute. Something like this, with a document saved from the API:

```python
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

transport = StdioTransport(
    command=".venv/bin/python",
    args=["main.py"],
    env={"MCP_TRANSPORT": "stdio", "OPENAPI_SCHEMA_PATH": "…", "AUTH_TOKEN": "…",
         "OPENOPS_API_URL": "http://localhost:3000", "PATH": os.environ["PATH"]},
)
```

Assert behaviour, not shape. `assert names == {...}` beats `assert tools is not None`.

## Things that will bite you

**stdout is the protocol on stdio.** Never print, and never add a logging handler that writes
to stdout. A single line corrupts the stream and the client drops the session with a JSON-RPC
parse error that points nowhere near the cause.

**`.env` fills in what the environment omits.** `load_dotenv()` does not override exported
variables, but it does supply missing ones — which is why a checkout configured for `http` once
hijacked a process spawned to speak `stdio`, and why the API now passes `MCP_TRANSPORT`
explicitly. Prefer exported variables in tests and scripts.

**The `project_id` header must never reach the API.** The API reads the project from the token
claim and would *ignore* a stray header rather than reject it, so a leak acts in the wrong
project silently. That strip has a dedicated integration test; do not weaken it.

**Configuration is validated once, at startup.** Add new settings to `config.py` with a clear
`ConfigError`, rather than reading `os.environ` deeper in the code.

**`requirements.txt` is generated.** Never edit it. After a dependency change run `uv lock`
then the export command in the README, and commit `pyproject.toml`, `uv.lock` and
`requirements.txt` together.

## Code style

- Formatting and linting are `ruff`'s decision — run it rather than arguing with it.
- Type everything; `mypy` runs strict. Prefer a narrow local type over `Any`.
- Comments explain **why**, never what. If a comment restates the code, delete it. If it
  records a decision someone would otherwise undo, keep it.
- Module docstrings say what the module is for. Private helpers usually need nothing.
- Do not add defensive checks for states the code has already guaranteed — `_validate` proves
  `paths` is a mapping, so nothing downstream re-checks it.

## Making a change

1. Confirm it belongs in this repository (see the table above).
2. Write the failing test.
3. Make it pass.
4. Run `pytest`, `mypy`, `ruff`.
5. If the stdio path is involved, spawn the process and confirm it end to end.
6. Update `README.md` when behaviour or configuration changes, and
   `docs/architecture.md` when a decision changes.

## Pull requests

Single-purpose, scoped to one issue, and referencing its Linear issue in the body
(`Fixes OPS-1234.`). Titles are imperative, capitalised, three words or more. Commit messages
say what changed and why, never how.
