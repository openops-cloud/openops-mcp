"""Entrypoint: read configuration, build the tool surface, serve it."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

from .auth import static
from .config import ConfigError, HttpSettings, Settings, StdioSettings, load_settings
from .logging_config import setup_logging
from .openapi import fetch_spec, read_spec
from .server import build_server

logger = setup_logging()

SPEC_FETCH_TIMEOUT = 15.0


async def _fetch_spec(settings: Settings) -> dict[str, Any]:
    """Read the OpenAPI document with its own short-lived client.

    Separate from the client the tools use: this runs before the server's event loop
    exists, and an httpx client should not be shared across loops.
    """
    async with httpx.AsyncClient(timeout=SPEC_FETCH_TIMEOUT) as client:
        return await fetch_spec(settings.common.openapi_url, client)


def _load_spec(settings: Settings) -> dict[str, Any]:
    """The document the API filtered for this profile, from a file or over HTTP."""
    if settings.common.openapi_path is not None:
        return read_spec(settings.common.openapi_path)

    return asyncio.run(_fetch_spec(settings))


def build(settings: Settings) -> FastMCP:
    """Build the server for the configured transport."""
    spec = _load_spec(settings)

    if isinstance(settings, StdioSettings):
        return build_server(
            spec=spec,
            client=static.build_api_client(settings.common.api_url, settings.auth_token),
        )

    from .auth import oauth

    return build_server(
        spec=spec,
        client=oauth.build_api_client(settings),
        auth=oauth.build_auth_provider(settings),
    )


def main() -> None:
    load_dotenv()

    try:
        settings = load_settings()
        server = build(settings)
    except ConfigError as error:
        # Configuration faults are the operator's to fix, so they are reported as a
        # single clear line rather than a traceback.
        logger.error("Cannot start: %s", error)
        sys.exit(1)

    if isinstance(settings, HttpSettings):
        logger.info("Serving MCP over HTTP on %s:%d", settings.host, settings.port)
        server.run(
            transport="http",
            host=settings.host,
            port=settings.port,
            # Each request is independent, so a request is authorized by the token it
            # carries rather than by whichever token opened the session.
            stateless_http=True,
        )
    else:
        logger.info("Serving MCP over stdio")
        server.run()


if __name__ == "__main__":
    main()
