"""Reading the API's MCP document and turning it into a tool surface.

The document arrives already filtered to one profile, so this module neither chooses nor
prunes operations — the API decides, and the two sides cannot disagree about the list.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastmcp.server.providers.openapi import MCPType, RouteMap

from .config import ConfigError

logger = logging.getLogger(__name__)


def _validate(spec: Any, source: str) -> dict[str, Any]:
    if not isinstance(spec, dict) or "paths" not in spec:
        raise ConfigError(f"{source} returned no OpenAPI 'paths'")

    logger.info("Loaded %d paths from %s", len(spec["paths"]), source)

    return spec


async def fetch_spec(url: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Read the document over HTTP. The endpoint is public, so no credential is needed."""
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ConfigError(f"could not fetch the OpenAPI document from {url}: {exc}") from exc

    try:
        spec = response.json()
    except ValueError as exc:
        raise ConfigError(f"{url} did not return JSON: {exc}") from exc

    return _validate(spec, url)


def read_spec(path: str) -> dict[str, Any]:
    """Read a document the API wrote for this process, rather than fetching it.

    Used on stdio, where a process is spawned per chat request: reading the file the API
    just wrote costs nothing, while an HTTP round trip per spawn would.
    """
    file_path = Path(path)

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read the OpenAPI document at {file_path}: {exc}") from exc

    try:
        spec = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{file_path} is not valid JSON: {exc}") from exc

    return _validate(spec, str(file_path))


def build_route_maps() -> list[RouteMap]:
    """Every operation in the document becomes a tool.

    The document is the allow-list, so there is nothing left to exclude here. Stated
    explicitly rather than relying on FastMCP's default, so the intent survives a version
    bump.
    """
    return [RouteMap(methods="*", pattern=r".*", mcp_type=MCPType.TOOL)]
