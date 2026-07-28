"""RFC 8693 token exchange.

A client's token is addressed to this server, not to the OpenOps API, so it is never
forwarded. It is presented here and exchanged for a separate API-audience token.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

# Long enough that a burst of tool calls costs one exchange, short enough that a
# revoked connection stops working promptly: the authorization server checks the grant
# on every exchange, so this is the window in which a revocation is not yet noticed.
MAX_CACHE_SECONDS = 60.0

# Discard a cached token slightly before it expires, so one is never used in the
# instant between the check and the downstream request.
EXPIRY_MARGIN_SECONDS = 5.0


class ExchangeError(Exception):
    """The exchange did not yield a token, so the request must not proceed."""


@dataclass(frozen=True)
class _Entry:
    token: str
    expires_at: float


class TokenExchanger:
    """Exchanges verified client tokens for API tokens, with a short-lived cache."""

    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._token_endpoint = token_endpoint
        self._auth = httpx.BasicAuth(client_id, client_secret)
        self._http = http_client
        self._cache: dict[str, _Entry] = {}

    async def exchange(self, subject_token: str) -> str:
        """Return an API-audience token for this client token, or raise ExchangeError."""
        key = hashlib.sha256(subject_token.encode()).hexdigest()
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached and cached.expires_at > now:
            return cached.token

        token, expires_in = await self._request(subject_token)
        self._cache[key] = _Entry(
            token=token,
            expires_at=now + min(MAX_CACHE_SECONDS, max(expires_in - EXPIRY_MARGIN_SECONDS, 0.0)),
        )

        return token

    async def _request(self, subject_token: str) -> tuple[str, float]:
        try:
            response = await self._http.post(
                self._token_endpoint,
                auth=self._auth,
                data={
                    "grant_type": TOKEN_EXCHANGE_GRANT,
                    "subject_token": subject_token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                },
            )
        except httpx.HTTPError as exc:
            raise ExchangeError(f"could not reach the authorization server: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            # The body carries an RFC 6749 error code, which is the useful part; the
            # token itself is never logged.
            detail = _describe(response)
            logger.warning("Token exchange refused: %s", detail)
            raise ExchangeError(f"the authorization server refused the exchange: {detail}")

        try:
            payload = response.json()
            return str(payload["access_token"]), float(payload.get("expires_in", 0))
        except (ValueError, KeyError, TypeError) as exc:
            raise ExchangeError(
                f"the authorization server returned no access token: {exc}"
            ) from exc

    def evict(self, subject_token: str) -> None:
        self._cache.pop(hashlib.sha256(subject_token.encode()).hexdigest(), None)


def _describe(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", "unknown_error")
        description = body.get("error_description")
        return f"{error}: {description}" if description else str(error)
    except ValueError:
        return f"HTTP {response.status_code}"
