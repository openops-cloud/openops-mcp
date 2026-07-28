"""Turning the API's OpenAPI document into exactly the allowed set of tools."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx
from fastmcp.server.providers.openapi import MCPType, RouteMap

from .config import ConfigError
from .routes import HttpMethod, RouteSpec

# FastMCP matches on upper-case verbs; the allow-list and OpenAPI both use lower-case.
_ROUTE_MAP_METHOD: dict[HttpMethod, Any] = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
}

logger = logging.getLogger(__name__)


async def fetch_spec(url: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Read the OpenAPI document. The endpoint is public, so no credential is needed."""
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ConfigError(f"could not fetch the OpenAPI document from {url}: {exc}") from exc

    try:
        spec = response.json()
    except ValueError as exc:
        raise ConfigError(f"{url} did not return JSON: {exc}") from exc

    if not isinstance(spec, dict) or "paths" not in spec:
        raise ConfigError(f"{url} returned no OpenAPI 'paths'")

    return spec


def prune_spec(
    spec: dict[str, Any], routes: list[RouteSpec]
) -> tuple[dict[str, Any], list[RouteSpec]]:
    """Reduce the document to the allowed operations.

    Returns the pruned document and the entries that matched nothing, so a mistyped
    path is reported rather than silently producing no tool.
    """
    allowed = {route.path: route.methods for route in routes}
    spec_paths: dict[str, Any] = spec.get("paths") or {}

    kept: dict[str, Any] = {}
    matched: set[tuple[str, str]] = set()

    for path, operations in spec_paths.items():
        if path not in allowed or not isinstance(operations, dict):
            continue

        selected = {
            method: operation
            for method, operation in operations.items()
            if method.lower() in allowed[path]
        }

        if selected:
            kept[path] = selected
            matched.update((path, method.lower()) for method in selected)

    unmatched = [
        route
        for route in routes
        if not any((route.path, method) in matched for method in route.methods)
    ]

    pruned = {**spec, "paths": kept}
    _log_unlisted(spec_paths, allowed)

    return pruned, unmatched


def assert_all_matched(unmatched: list[RouteSpec], source: str) -> None:
    """Refuse to start when the allow-list names operations the API does not expose."""
    if not unmatched:
        return

    listed = ", ".join(
        f"{'|'.join(sorted(m.upper() for m in route.methods))} {route.path}" for route in unmatched
    )
    raise ConfigError(
        f"{source} lists operations the API does not expose: {listed}. "
        "Check the paths and methods against the OpenAPI document."
    )


def build_route_maps(routes: list[RouteSpec]) -> list[RouteMap]:
    """Map allowed operations to tools, and exclude everything else.

    The trailing exclude matters: FastMCP turns every remaining route into a tool by
    default, so without it a pruning mistake would quietly expose an operation.
    """
    maps = [
        RouteMap(
            methods=sorted(_ROUTE_MAP_METHOD[method] for method in route.methods),
            pattern=f"^{_escape(route.path)}$",
            mcp_type=MCPType.TOOL,
        )
        for route in routes
    ]
    maps.append(RouteMap(methods="*", pattern=r".*", mcp_type=MCPType.EXCLUDE))

    return maps


def build_mcp_names(spec: dict[str, Any], routes: list[RouteSpec]) -> dict[str, str]:
    """Map operationId to the tool name requested in the allow-list."""
    overrides = {route.path: route.name for route in routes if route.name}
    if not overrides:
        return {}

    names: dict[str, str] = {}
    for path, operations in (spec.get("paths") or {}).items():
        if path not in overrides or not isinstance(operations, dict):
            continue
        for operation in operations.values():
            operation_id = (operation or {}).get("operationId")
            if operation_id:
                names[operation_id] = overrides[path]

    return names


def _escape(path: str) -> str:
    """Escape a path for use in a regex, leaving OpenAPI `{param}` segments intact."""
    import re

    return re.escape(path).replace(r"\{", "{").replace(r"\}", "}")


def _log_unlisted(spec_paths: Mapping[str, Any], allowed: Mapping[str, frozenset[Any]]) -> None:
    unlisted = sorted(set(spec_paths) - set(allowed))
    if unlisted:
        logger.debug(
            "%d API operations are not in the allow-list and will not become tools: %s",
            len(unlisted),
            ", ".join(unlisted),
        )
