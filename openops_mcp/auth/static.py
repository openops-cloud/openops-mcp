"""stdio authentication: one token for the life of the process.

The API spawns this server per chat request with a short-lived token belonging to the
signed-in user, so a single header is the whole story — there is no second identity
to distinguish.
"""

from __future__ import annotations

import httpx


def build_api_client(api_url: str, auth_token: str, timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=api_url,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=timeout,
    )
