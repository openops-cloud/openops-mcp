"""Does a downstream request see the token of the request being served?

Everything about HTTP mode rests on this. The tools are generated from an OpenAPI
document and share one httpx client, so the only place a per-user credential can be
attached is a request hook — and that only works if the hook runs inside the async
context of the request being served. If it instead saw whichever token arrived first,
two users would act as each other.

These tests drive a real HTTP server over a real socket, because the property under
test is a property of the transport.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from openops_mcp.auth.oauth import _caller_token
from openops_mcp.openapi import prune_spec
from openops_mcp.routes import RouteSpec
from openops_mcp.server import build_server

ISSUER = "https://auth.example.com"
RESOURCE = "https://mcp.example.com"

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "OpenOps", "version": "0.0.0"},
    "paths": {
        "/v1/flows/": {
            "get": {
                "operationId": "listFlows",
                "summary": "List flows",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}

ROUTES = [RouteSpec(path="/v1/flows/", methods=frozenset({"get"}), name="list_flows")]


@pytest.fixture(scope="module")
def keys() -> RSAKeyPair:
    return RSAKeyPair.generate()


def token_for(keys: RSAKeyPair, subject: str) -> str:
    return keys.create_token(
        subject=subject,
        issuer=ISSUER,
        audience=RESOURCE,
        scopes=["mcp"],
        additional_claims={"project_id": f"project-of-{subject}"},
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def serve(app: Any, port: int) -> AsyncIterator[None]:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.02)
        if task.done():
            task.result()

    try:
        yield
    finally:
        server.should_exit = True
        await task


class Recorder:
    """Records which caller each downstream request was authorized as."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []

    def hook(self) -> Any:
        async def record(request: httpx.Request) -> None:
            # Exactly what production reads, so this exercises the real mechanism.
            token = _caller_token()
            # Stand in for the exchange: the question is only *whose* token arrives.
            request.headers["Authorization"] = "Bearer exchanged"
            self.seen.append((request.url.path, _subject_of(token)))

        return record


def _subject_of(token: str) -> str | None:
    """Read `sub` for assertion purposes only; the server never does this."""
    import base64
    import json

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(payload))["sub"])


def build_http_app(recorder: Recorder, keys: RSAKeyPair) -> Any:
    downstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []})),
        base_url="http://api.internal",
        event_hooks={"request": [recorder.hook()]},
    )
    pruned, _ = prune_spec(SPEC, ROUTES)
    server = build_server(
        spec=pruned,
        routes=ROUTES,
        client=downstream,
        auth=RemoteAuthProvider(
            token_verifier=JWTVerifier(
                public_key=keys.public_key,
                issuer=ISSUER,
                audience=RESOURCE,
                required_scopes=["mcp"],
            ),
            authorization_servers=[ISSUER],  # type: ignore[list-item]
            base_url=RESOURCE,
            resource_name="OpenOps",
        ),
    )

    return server.http_app(stateless_http=True)


async def test_each_request_is_authorized_with_its_own_token(keys: RSAKeyPair) -> None:
    recorder = Recorder()
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"

    async for _ in serve(build_http_app(recorder, keys), port):

        async def call(subject: str) -> None:
            transport_auth = BearerAuth(token_for(keys, subject))
            async with Client(url, auth=transport_auth) as client:
                await client.call_tool("list_flows", {})

        # Concurrently, so a shared or session-frozen token would show up as one
        # subject appearing twice.
        await asyncio.gather(call("alice"), call("bob"))

    subjects = sorted(subject for _, subject in recorder.seen)
    assert subjects == ["alice", "bob"], (
        f"each downstream request must carry its own caller; saw {recorder.seen}"
    )


async def test_sequential_requests_do_not_reuse_the_first_token(keys: RSAKeyPair) -> None:
    recorder = Recorder()
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"

    async for _ in serve(build_http_app(recorder, keys), port):
        for subject in ("alice", "bob", "alice"):
            async with Client(url, auth=BearerAuth(token_for(keys, subject))) as client:
                await client.call_tool("list_flows", {})

    assert [subject for _, subject in recorder.seen] == ["alice", "bob", "alice"]


async def test_an_unauthenticated_request_is_refused(keys: RSAKeyPair) -> None:
    recorder = Recorder()
    port = free_port()

    async for _ in serve(build_http_app(recorder, keys), port):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code == httpx.codes.UNAUTHORIZED
    # RFC 9728: the challenge tells the client where to find the metadata.
    assert "resource_metadata" in response.headers.get("WWW-Authenticate", "")
    assert recorder.seen == []


async def test_a_token_for_another_audience_is_refused(keys: RSAKeyPair) -> None:
    recorder = Recorder()
    port = free_port()
    # An API-audience token must not be accepted here, or this server would launder it.
    wrong_audience = keys.create_token(
        subject="alice", issuer=ISSUER, audience=ISSUER, scopes=["mcp"]
    )

    async for _ in serve(build_http_app(recorder, keys), port):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {wrong_audience}",
                },
            )

    assert response.status_code == httpx.codes.UNAUTHORIZED
    assert recorder.seen == []


async def test_a_token_without_the_mcp_scope_is_refused(keys: RSAKeyPair) -> None:
    recorder = Recorder()
    port = free_port()
    no_scope = keys.create_token(subject="alice", issuer=ISSUER, audience=RESOURCE, scopes=[])

    async for _ in serve(build_http_app(recorder, keys), port):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {no_scope}",
                },
            )

    assert response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN)
    assert recorder.seen == []
