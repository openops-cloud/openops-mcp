"""RFC 8693 token exchange.

A client's token is addressed to this server, not to the OpenOps API, so it is never
forwarded. It is presented here and exchanged for a separate API-audience token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

# A ceiling on how long an exchanged token is reused, rather than a policy: the lifetime
# the authorization server reports governs, and this only stops an implausible one from
# pinning a token in memory for hours.
#
# Reuse does not delay revocation. The API re-checks the grant, the user's status and
# their project access on every request it serves, so a cached token stops being accepted
# the moment a connection is revoked — and `evict` drops it as soon as that surfaces as a
# 401. That is what lets this window be a useful length rather than a cautious one.
MAX_CACHE_SECONDS = 900.0

# Discard a cached token slightly before it expires, so one is never used in the
# instant between the check and the downstream request.
EXPIRY_MARGIN_SECONDS = 5.0

# Reclaim expired entries once the cache reaches this size. Caller tokens rotate, so
# every entry eventually becomes garbage; without a bound a long-running process
# accumulates dead tokens for as long as it runs.
CACHE_SWEEP_THRESHOLD = 10_000


class ExchangeError(Exception):
    """The exchange did not yield a token, so the request must not proceed."""


@dataclass(frozen=True)
class _Entry:
    token: str
    expires_at: float


class TokenExchanger:
    """Exchanges verified client tokens for API tokens, caching and coalescing the work."""

    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        http_client: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_endpoint = token_endpoint
        self._auth = httpx.BasicAuth(client_id, client_secret)
        self._http = http_client
        self._clock = clock
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Task[str]] = {}

    async def exchange(self, subject_token: str) -> str:
        """Return an API-audience token for this client token, or raise ExchangeError."""
        key = _key(subject_token)

        cached = self._cache.get(key)
        if cached and cached.expires_at > self._clock():
            return cached.token

        inflight = self._inflight.get(key)
        if inflight is None:
            inflight = asyncio.create_task(self._exchange_and_store(key, subject_token))
            self._inflight[key] = inflight

        # An agent calls its tools in bursts, so the misses that matter arrive together.
        # Shielded because one caller abandoning its request must not cancel the exchange
        # the others are waiting on.
        return await asyncio.shield(inflight)

    async def _exchange_and_store(self, key: str, subject_token: str) -> str:
        try:
            token, expires_in = await self._request(subject_token)
            self._remember(key, token, expires_in)
            return token
        finally:
            # In the `finally` rather than a done callback, so the entry is gone before
            # any waiter resumes: a failure must not leave the token stuck forever.
            self._inflight.pop(key, None)

    def _remember(self, key: str, token: str, expires_in: float) -> None:
        lifetime = min(MAX_CACHE_SECONDS, max(expires_in - EXPIRY_MARGIN_SECONDS, 0.0))
        if lifetime <= 0:
            return

        now = self._clock()

        if len(self._cache) >= CACHE_SWEEP_THRESHOLD:
            for expired in [k for k, entry in self._cache.items() if entry.expires_at <= now]:
                del self._cache[expired]

            # Nothing reclaimable: drop everything rather than grow. That costs
            # re-exchanging, which is slower but correct, and never unbounded memory.
            if len(self._cache) >= CACHE_SWEEP_THRESHOLD:
                logger.warning("Token cache full with live entries; clearing it")
                self._cache.clear()

        self._cache[key] = _Entry(token=token, expires_at=now + lifetime)

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
        """Forget this caller's token, so the next call exchanges again."""
        self._cache.pop(_key(subject_token), None)

    def cache_size(self) -> int:
        """Entries currently held, expired or not. For tests and diagnostics."""
        return len(self._cache)

    async def aclose(self) -> None:
        await self._http.aclose()


def _key(subject_token: str) -> str:
    """Key on the token, not the user: two tokens for one user may name different
    projects, and must not share an entry."""
    return hashlib.sha256(subject_token.encode()).hexdigest()


def _describe(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", "unknown_error")
        description = body.get("error_description")
        return f"{error}: {description}" if description else str(error)
    except ValueError:
        return f"HTTP {response.status_code}"
