import base64

import httpx
import pytest

from openops_mcp.auth.exchange import (
    ACCESS_TOKEN_TYPE,
    TOKEN_EXCHANGE_GRANT,
    ExchangeError,
    TokenExchanger,
)

ENDPOINT = "https://auth.example.com/v1/oauth/token"


def exchanger(handler: object, **kwargs: object) -> TokenExchanger:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return TokenExchanger(
        token_endpoint=ENDPOINT,
        client_id="openops-mcp-rs",
        client_secret="s" * 32,
        http_client=client,
        **kwargs,  # type: ignore[arg-type]
    )


def granting(token: str = "api-token", expires_in: int = 300) -> object:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


async def test_returns_the_api_token() -> None:
    assert await exchanger(granting()).exchange("caller-token") == "api-token"


async def test_sends_the_rfc_8693_grant_with_the_caller_token() -> None:
    handler = granting()

    await exchanger(handler).exchange("caller-token")

    body = dict(
        pair.split("=", 1)
        for pair in handler.calls[0].content.decode().split("&")  # type: ignore[attr-defined]
    )
    assert body["grant_type"] == TOKEN_EXCHANGE_GRANT.replace(":", "%3A")
    assert body["subject_token"] == "caller-token"
    assert body["subject_token_type"] == ACCESS_TOKEN_TYPE.replace(":", "%3A")


async def test_authenticates_as_the_resource_server() -> None:
    handler = granting()

    await exchanger(handler).exchange("caller-token")

    header = handler.calls[0].headers["authorization"]  # type: ignore[attr-defined]
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    assert decoded == f"openops-mcp-rs:{'s' * 32}"


async def test_never_forwards_the_caller_token_as_the_credential() -> None:
    handler = granting()

    await exchanger(handler).exchange("caller-token")

    # The caller's token is the subject of the exchange, never the credential for it.
    assert "caller-token" not in handler.calls[0].headers["authorization"]  # type: ignore[attr-defined]


async def test_reuses_a_cached_token_for_the_same_caller() -> None:
    handler = granting()
    subject = exchanger(handler)

    first = await subject.exchange("caller-token")
    second = await subject.exchange("caller-token")

    assert first == second
    assert len(handler.calls) == 1  # type: ignore[attr-defined]


async def test_does_not_share_a_token_between_callers() -> None:
    issued = iter(["token-for-alice", "token-for-bob"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": next(issued), "expires_in": 300})

    subject = exchanger(handler)

    assert await subject.exchange("alice-token") == "token-for-alice"
    assert await subject.exchange("bob-token") == "token-for-bob"


async def test_exchanges_again_once_the_entry_is_evicted() -> None:
    handler = granting()
    subject = exchanger(handler)

    await subject.exchange("caller-token")
    subject.evict("caller-token")
    await subject.exchange("caller-token")

    assert len(handler.calls) == 2  # type: ignore[attr-defined]


async def test_does_not_cache_a_token_that_expires_immediately() -> None:
    handler = granting(expires_in=0)
    subject = exchanger(handler)

    await subject.exchange("caller-token")
    await subject.exchange("caller-token")

    assert len(handler.calls) == 2  # type: ignore[attr-defined]


async def test_raises_when_the_authorization_server_refuses() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "the authorization for this client has been revoked",
            },
        )

    with pytest.raises(ExchangeError, match="revoked"):
        await exchanger(handler).exchange("caller-token")


async def test_raises_when_the_authorization_server_is_unreachable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ExchangeError, match="could not reach"):
        await exchanger(handler).exchange("caller-token")


async def test_raises_when_the_response_carries_no_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer"})

    with pytest.raises(ExchangeError, match="no access token"):
        await exchanger(handler).exchange("caller-token")


async def test_does_not_cache_a_failure() -> None:
    outcomes = iter([httpx.Response(503), httpx.Response(200, json={"access_token": "api-token"})])

    def handler(_: httpx.Request) -> httpx.Response:
        return next(outcomes)

    subject = exchanger(handler)

    with pytest.raises(ExchangeError):
        await subject.exchange("caller-token")

    # A transient failure must not poison the cache for the rest of the process.
    assert await subject.exchange("caller-token") == "api-token"
