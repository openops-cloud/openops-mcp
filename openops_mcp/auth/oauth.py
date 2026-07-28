"""HTTP authentication: verify the caller, then act as them against the API.

Inbound tokens are verified locally against the authorization server's published keys,
so no request costs a round trip to it. The verified token is then exchanged for a
separate API-audience token, which is what reaches the API — the caller's own token
never does.
"""

from __future__ import annotations

import logging

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

    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[settings.issuer],  # type: ignore[list-item]
        base_url=settings.resource_url,
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
        event_hooks={"request": [_authorize(exchanger)]},
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


def _authorize(exchanger: TokenExchanger):  # type: ignore[no-untyped-def]
    async def hook(request: httpx.Request) -> None:
        # Runs inside the tool call, so this resolves to the request being served rather
        # than whichever request opened the session.
        caller_token = _caller_token()

        # Fail closed. Letting the request continue unauthorized would surface as a
        # confusing 401 from the API and hide the real cause.
        api_token = await exchanger.exchange(caller_token)
        request.headers["Authorization"] = f"Bearer {api_token}"

        logger.debug("Authorized %s %s for the calling user", request.method, request.url.path)

    return hook
