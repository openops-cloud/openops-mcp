"""Assembling the OpenAPI document, the allow-list and an HTTP client into a server.

This is deliberately the only place a server gets built. Both transports come through
here with the same document and the same allow-list, so the tool surface cannot differ
between them — only the client's authentication does.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from .openapi import build_mcp_names, build_route_maps
from .routes import RouteSpec

logger = logging.getLogger(__name__)

SERVER_NAME = "OpenOps"


def build_server(
    *,
    spec: dict[str, Any],
    routes: list[RouteSpec],
    client: httpx.AsyncClient,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Build the server. `auth` is None for stdio, where the transport is the boundary."""
    descriptions = {route.path: route.description for route in routes if route.description}

    def apply_overrides(route: Any, component: Any) -> None:
        path = getattr(route, "path", None)
        described = descriptions.get(path) if isinstance(path, str) else None
        if described:
            component.description = described

    settings: dict[str, Any] = {}
    if auth is not None:
        settings["auth"] = auth

    server: FastMCP = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name=SERVER_NAME,
        route_maps=build_route_maps(routes),
        mcp_names=build_mcp_names(spec, routes),
        mcp_component_fn=apply_overrides if descriptions else None,
        tags={"openops"},
        **settings,
    )

    logger.info(
        "Built %s from %d allowed operations across %d paths",
        SERVER_NAME,
        sum(len(route.methods) for route in routes),
        len(routes),
    )

    return server
