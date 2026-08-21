"""HTTP authentication: verify the caller, then act as them against the API.

Inbound tokens are verified locally against the authorization server's published keys,
so no request costs a round trip to it. The verified token is then exchanged for a
separate API-audience token, which is what reaches the API — the caller's own token
never does.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastmcp.server.auth import AuthProvider, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_http_headers

from ..config import CLIENT_ID, HttpSettings
from .exchange import ExchangeError, TokenExchanger

logger = logging.getLogger(__name__)

REQUIRED_SCOPE = "mcp"
API_TIMEOUT_SECONDS = 30.0
BEARER_PREFIX = "bearer "


def build_auth_provider(settings: HttpSettings) -> AuthProvider:
    """Verify tokens locally, and publish where they should be obtained.

    RemoteAuthProvider serves the RFC 9728 protected-resource metadata and the
    WWW-Authenticate challenge, which is how a client discovers the authorization
    server after its first unauthenticated request.
    """
    verifier = JWTVerifier(
        jwks_uri=settings.jwks_uri,
        issuer=settings.issuer,
        # A token minted for the API carries a different audience and is refused here,
        # so this server cannot be used to launder one.
        audience=settings.resource_url,
        required_scopes=[REQUIRED_SCOPE],
    )

    # `base_url` is this server's origin, not the resource identifier: FastMCP appends
    # the transport's mount path to derive the resource and the metadata location.
    # Passing the full resource URI here would double that path segment, and the
    # challenge would point clients at metadata that does not exist.
    parsed = urlparse(settings.resource_url)

    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[settings.issuer],  # type: ignore[list-item]
        base_url=f"{parsed.scheme}://{parsed.netloc}",
        resource_name="OpenOps",
    )


def build_api_client(settings: HttpSettings) -> httpx.AsyncClient:
    """An API client that authorizes each request as whoever made it."""
    exchanger = TokenExchanger(
        token_endpoint=settings.token_endpoint,
        client_id=CLIENT_ID,
        client_secret=settings.client_secret,
        http_client=httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS),
    )

    return httpx.AsyncClient(
        base_url=settings.common.api_url,
        timeout=API_TIMEOUT_SECONDS,
        transport=_AuthorizingTransport(exchanger=exchanger, inner=httpx.AsyncHTTPTransport()),
    )


def _caller_token() -> str:
    """The bearer token on the request being served.

    Read from the request headers rather than `get_access_token()`, which returns None
    during tool execution in FastMCP 3.4.5 — the auth context does not reach the task
    the tool runs in, while the HTTP request context does.

    This is not a way around verification. A request only reaches a tool after the auth
    provider has checked the token's signature, issuer, audience and required scopes, so
    this reads a credential that has already been validated. The security boundary is
    also not here: the authorization server verifies the subject token again during the
    exchange below, and refuses it unless the grant and the user are still active. A
    token that somehow arrived unverified would be rejected there.
    """
    header = get_http_headers(include_all=True).get("authorization", "")

    if not header.lower().startswith(BEARER_PREFIX):
        raise ExchangeError("the request carries no bearer token")

    token = header[len(BEARER_PREFIX) :].strip()
    if not token:
        raise ExchangeError("the request carries an empty bearer token")

    return token


class _AuthorizingTransport(httpx.AsyncBaseTransport):
    """Authorizes every API request as its caller, and recovers from a stale token.

    A transport rather than a request event hook because a hook cannot retry, and one
    retry is what turns a revoked connection into a message worth reading.
    """

    def __init__(self, *, exchanger: TokenExchanger, inner: httpx.AsyncBaseTransport) -> None:
        self._exchanger = exchanger
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Runs inside the tool call, so this resolves to the request being served rather
        # than whichever request opened the session.
        caller_token = _caller_token()

        response = await self._send(request, caller_token)
        if response.status_code != httpx.codes.UNAUTHORIZED:
            return response

        # The API rejected the token we hold, so it is worthless: drop it whether or not
        # this request can be retried, and never present it again.
        self._exchanger.evict(caller_token)

        if not _is_replayable(request):
            return response

        await response.aclose()
        logger.info("The API refused the exchanged token; exchanging again")

        # A second attempt goes back to the authorization server, which is the only place
        # that can say *why* — a revoked grant raises ExchangeError with the real reason
        # instead of handing the model an unexplained 401.
        retried = await self._send(request, caller_token)

        if retried.status_code == httpx.codes.UNAUTHORIZED:
            # A fresh token was refused as well, so it is worth no more than the one it
            # replaced. Keeping it would mean opening the next call with a credential the
            # API has already rejected.
            self._exchanger.evict(caller_token)

        return retried

    async def _send(self, request: httpx.Request, caller_token: str) -> httpx.Response:
        # Fail closed. Letting the request continue unauthorized would surface as a
        # confusing 401 from the API and hide the real cause.
        api_token = await self._exchanger.exchange(caller_token)
        request.headers["Authorization"] = f"Bearer {api_token}"

        logger.debug("Authorized %s %s for the calling user", request.method, request.url.path)

        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
        await self._exchanger.aclose()


def _is_replayable(request: httpx.Request) -> bool:
    """Only a request whose body is already in memory can be sent twice."""
    try:
        _ = request.content
    except httpx.RequestNotRead:
        return False
    return True
