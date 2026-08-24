"""Assembling the API's MCP document and an HTTP client into a server.

This is deliberately the only place a server gets built. Both transports come through
here with the same document, so the tool surface cannot differ between them — only the
client's authentication does.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from .openapi import build_route_maps

SERVER_NAME = "OpenOps"


def build_server(
    *,
    spec: dict[str, Any],
    client: httpx.AsyncClient,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Build the server. `auth` is None for stdio, where the transport is the boundary."""
    return FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name=SERVER_NAME,
        route_maps=build_route_maps(),
        tags={"openops"},
        auth=auth,
        # The API's generated response schemas do not always mark nullable fields, so
        # validating against them turns a healthy 200 into `None is not of type 'string'`
        # for a field the API legitimately omits.
        validate_output=False,
    )
