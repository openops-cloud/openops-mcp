"""The transport that authorizes each API call as the caller being served.

The interesting behaviour is recovery: a cached token that the API no longer accepts must
turn into the authorization server's real answer, not an opaque 401 handed to the model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from openops_mcp.auth import oauth
from openops_mcp.auth.exchange import ExchangeError, TokenExchanger

CALLER_TOKEN = "caller-token"


@pytest.fixture(autouse=True)
def caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the request context, which only exists while serving a request."""
    monkeypatch.setattr(oauth, "_caller_token", lambda: CALLER_TOKEN)


class AuthServer:
    """An authorization server that issues a distinct token per exchange."""

    def __init__(self, refuse_after: int | None = None) -> None:
        self.exchanges = 0
        self._refuse_after = refuse_after

    def handler(self, _: httpx.Request) -> httpx.Response:
        self.exchanges += 1
        if self._refuse_after is not None and self.exchanges > self._refuse_after:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200, json={"access_token": f"api-token-{self.exchanges}", "expires_in": 900}
        )


class Api:
    """An API that records the credential it was presented."""

    def __init__(self, *statuses: int) -> None:
        self.presented: list[str] = []
        self._statuses = list(statuses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.presented.append(request.headers.get("authorization", ""))
        status = self._statuses.pop(0) if self._statuses else httpx.codes.OK
        return httpx.Response(status, json={"data": []})


def build(auth_server: AuthServer, api: Api) -> tuple[httpx.AsyncClient, TokenExchanger]:
    exchanger = TokenExchanger(
        token_endpoint="https://auth.example.com/v1/oauth/token",
        client_id="openops-mcp-rs",
        client_secret="s" * 32,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(auth_server.handler)),
    )
    client = httpx.AsyncClient(
        base_url="http://api.internal",
        transport=oauth._AuthorizingTransport(
            exchanger=exchanger, inner=httpx.MockTransport(api.handler)
        ),
    )
    return client, exchanger


async def test_presents_the_exchanged_token_not_the_callers() -> None:
    api = Api()
    client, _ = build(AuthServer(), api)

    response = await client.get("/v1/flows/")

    assert response.status_code == httpx.codes.OK
    assert api.presented == ["Bearer api-token-1"]


async def test_reuses_the_cached_token_across_calls() -> None:
    auth_server, api = AuthServer(), Api()
    client, _ = build(auth_server, api)

    await client.get("/v1/flows/")
    await client.get("/v1/flows/")

    assert auth_server.exchanges == 1
    assert api.presented == ["Bearer api-token-1", "Bearer api-token-1"]


async def test_exchanges_again_when_the_api_refuses_the_cached_token() -> None:
    auth_server = AuthServer()
    api = Api(httpx.codes.UNAUTHORIZED, httpx.codes.OK)
    client, _ = build(auth_server, api)

    # Warm the cache, then let the API reject what it holds.
    await client.get("/v1/flows/")
    response = await client.get("/v1/flows/")

    assert auth_server.exchanges == 2
    assert api.presented[-1] == "Bearer api-token-2"
    assert response.status_code == httpx.codes.OK


async def test_reports_the_authorization_servers_refusal_rather_than_a_bare_401() -> None:
    # The connection was revoked: the API stops accepting the cached token, and the
    # exchange is where the reason can actually be read.
    auth_server = AuthServer(refuse_after=1)
    api = Api(httpx.codes.OK, httpx.codes.UNAUTHORIZED)
    client, _ = build(auth_server, api)

    await client.get("/v1/flows/")

    with pytest.raises(ExchangeError, match="invalid_grant"):
        await client.get("/v1/flows/")


async def test_retries_at_most_once() -> None:
    auth_server = AuthServer()
    api = Api(httpx.codes.UNAUTHORIZED, httpx.codes.UNAUTHORIZED, httpx.codes.UNAUTHORIZED)
    client, _ = build(auth_server, api)

    response = await client.get("/v1/flows/")

    assert response.status_code == httpx.codes.UNAUTHORIZED
    assert len(api.presented) == 2, "a persistent 401 must not loop"


async def test_forgets_a_rejected_token_even_when_it_cannot_retry() -> None:
    auth_server = AuthServer()
    api = Api(httpx.codes.UNAUTHORIZED, httpx.codes.UNAUTHORIZED)
    client, exchanger = build(auth_server, api)

    await client.get("/v1/flows/")

    # Whether or not the retry helped, a token the API refuses must not be presented
    # again on the next call.
    assert exchanger.cache_size() == 0


def test_a_json_body_can_be_replayed() -> None:
    request = httpx.Request("POST", "http://api.internal/v1/blocks/options", json={"a": 1})

    assert oauth._is_replayable(request) is True


def test_a_streamed_body_cannot_be_replayed() -> None:
    async def streamed() -> AsyncIterator[bytes]:
        yield b'{"projectId":"p"}'

    # Consumed as it is sent, so a second attempt would transmit an empty body. Such a
    # request is returned as-is rather than retried.
    request = httpx.Request("POST", "http://api.internal/v1/blocks/options", content=streamed())

    assert oauth._is_replayable(request) is False


async def test_a_failed_exchange_stops_the_request() -> None:
    api = Api()
    client, _ = build(AuthServer(refuse_after=0), api)

    with pytest.raises(ExchangeError):
        await client.get("/v1/flows/")

    assert api.presented == [], "an unauthorized request must never reach the API"


async def test_closing_the_client_closes_the_exchangers_client() -> None:
    exchanger_closed: dict[str, Any] = {}

    auth_server, api = AuthServer(), Api()
    client, exchanger = build(auth_server, api)
    original = exchanger.aclose

    async def record() -> None:
        exchanger_closed["yes"] = True
        await original()

    exchanger.aclose = record  # type: ignore[method-assign]

    await client.aclose()

    assert exchanger_closed == {"yes": True}
