import asyncio
import base64
import urllib.parse

import httpx
import pytest

from openops_mcp.auth.exchange import (
    ACCESS_TOKEN_TYPE,
    CACHE_SWEEP_THRESHOLD,
    EXPIRY_MARGIN_SECONDS,
    MAX_CACHE_SECONDS,
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


# ---------------------------------------------------------------- single-flight ---


def slow_granting(delay: float = 0.05, expires_in: int = 300) -> object:
    """Yields to the event loop like a real exchange, so concurrency is observable."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(delay)
        return httpx.Response(
            200, json={"access_token": f"api-token-{len(calls)}", "expires_in": expires_in}
        )

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


async def test_collapses_concurrent_misses_for_one_caller_into_a_single_exchange() -> None:
    handler = slow_granting()
    subject = exchanger(handler)

    tokens = await asyncio.gather(*(subject.exchange("caller-token") for _ in range(20)))

    # A burst of tool calls is the case the cache exists for; without single-flight every
    # one of them misses and hits the authorization server.
    assert len(handler.calls) == 1  # type: ignore[attr-defined]
    assert set(tokens) == {"api-token-1"}


async def test_does_not_collapse_concurrent_exchanges_for_different_callers() -> None:
    handler = slow_granting()
    subject = exchanger(handler)

    await asyncio.gather(subject.exchange("alice-token"), subject.exchange("bob-token"))

    assert len(handler.calls) == 2  # type: ignore[attr-defined]


async def test_a_failed_exchange_reaches_every_waiting_caller() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(400, json={"error": "invalid_grant"})

    subject = exchanger(handler)

    results = await asyncio.gather(
        *(subject.exchange("caller-token") for _ in range(5)), return_exceptions=True
    )

    assert all(isinstance(result, ExchangeError) for result in results)


async def test_a_failed_exchange_does_not_block_the_next_attempt() -> None:
    outcomes = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"access_token": "api-token", "expires_in": 300}),
        ]
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return next(outcomes)

    subject = exchanger(handler)

    with pytest.raises(ExchangeError):
        await subject.exchange("caller-token")

    # The in-flight entry must be cleared on failure, or the token is stuck forever.
    assert await subject.exchange("caller-token") == "api-token"


async def test_one_caller_giving_up_does_not_cancel_the_exchange_for_the_others() -> None:
    handler = slow_granting(delay=0.1)
    subject = exchanger(handler)

    async def wait() -> str:
        return await subject.exchange("caller-token")

    quitter = asyncio.create_task(wait())
    stayer = asyncio.create_task(wait())
    await asyncio.sleep(0.02)
    quitter.cancel()

    assert await stayer == "api-token-1"


# ------------------------------------------------------------------- cache size ---


async def test_sweeps_expired_entries_instead_of_growing_without_bound() -> None:
    now = 1000.0
    handler = granting(expires_in=300)
    subject = exchanger(handler, clock=lambda: now)

    for index in range(CACHE_SWEEP_THRESHOLD):
        await subject.exchange(f"caller-{index}")
    assert subject.cache_size() == CACHE_SWEEP_THRESHOLD

    # Every entry is now expired, so the next insert should reclaim all of them.
    now += 301
    await subject.exchange("one-more")

    assert subject.cache_size() == 1


async def test_clears_the_cache_when_nothing_can_be_reclaimed() -> None:
    handler = granting(expires_in=300)
    subject = exchanger(handler, clock=lambda: 1000.0)

    for index in range(CACHE_SWEEP_THRESHOLD + 1):
        await subject.exchange(f"caller-{index}")

    # All live, so a sweep frees nothing: drop everything rather than grow. The cost is
    # re-exchanging, which is correct but slower — never unbounded memory.
    assert subject.cache_size() == 1


# ------------------------------------------------------------------------- ttl ---


async def test_reuses_a_token_for_its_full_lifetime_less_the_margin() -> None:
    now = 1000.0
    handler = granting(expires_in=300)
    subject = exchanger(handler, clock=lambda: now)

    await subject.exchange("caller-token")
    now += 300 - EXPIRY_MARGIN_SECONDS - 1
    await subject.exchange("caller-token")

    # Revocation is enforced by the API on every request, so the exchange does not need
    # repeating just to notice one.
    assert len(handler.calls) == 1  # type: ignore[attr-defined]


async def test_stops_reusing_a_token_before_it_expires() -> None:
    now = 1000.0
    handler = granting(expires_in=300)
    subject = exchanger(handler, clock=lambda: now)

    await subject.exchange("caller-token")
    now += 300 - EXPIRY_MARGIN_SECONDS
    await subject.exchange("caller-token")

    assert len(handler.calls) == 2  # type: ignore[attr-defined]


async def test_never_holds_a_token_longer_than_the_ceiling() -> None:
    now = 1000.0
    # A malformed or over-generous `expires_in` must not pin a token in memory for hours.
    handler = granting(expires_in=86_400)
    subject = exchanger(handler, clock=lambda: now)

    await subject.exchange("caller-token")
    now += MAX_CACHE_SECONDS
    await subject.exchange("caller-token")

    assert len(handler.calls) == 2  # type: ignore[attr-defined]


async def test_names_the_requested_project_in_the_exchange() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx.Response(200, json={"access_token": "api-token", "expires_in": 300})

    await exchanger(handler).exchange("caller-token", project_id="project-9")

    assert seen[0]["project_id"] == "project-9"


async def test_omits_the_project_when_none_is_asked_for() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx.Response(200, json={"access_token": "api-token", "expires_in": 300})

    await exchanger(handler).exchange("caller-token")

    # Absent, not empty: the authorization server defaults to the subject token's project.
    assert "project_id" not in seen[0]


async def test_does_not_share_a_token_between_projects() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"access_token": f"api-token-{calls}", "expires_in": 300}
        )

    subject = exchanger(handler)

    first = await subject.exchange("caller-token", project_id="project-a")
    second = await subject.exchange("caller-token", project_id="project-b")
    again = await subject.exchange("caller-token", project_id="project-a")

    # A token minted for one project must never authorize a call meant for another.
    assert first != second
    assert again == first
    assert calls == 2


async def test_evicting_a_caller_drops_every_project_it_holds() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"access_token": f"api-token-{calls}", "expires_in": 300}
        )

    subject = exchanger(handler)
    await subject.exchange("caller-token", project_id="project-a")
    await subject.exchange("caller-token", project_id="project-b")
    await subject.exchange("other-caller", project_id="project-a")

    subject.evict("caller-token")

    # A 401 means the caller's credential is dead, whichever project it was acting in.
    assert subject.cache_size() == 1
