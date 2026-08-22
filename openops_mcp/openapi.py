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

MCP_EXTENSION_KEY = "x-openops-mcp"

# The argument an agent uses to name where it is acting. A header because that is the only
# OpenAPI parameter location that both reaches the tool's input schema and can be removed
# from the request before it leaves this process — the API must never see it.
PROJECT_PARAMETER = "project_id"

# Kept to one sentence on purpose: it repeats in every tool's schema, so a second sentence
# costs a few hundred tokens of context on every request the model makes. The workflow
# belongs in the project-listing tool's own description.
PROJECT_PARAMETER_DESCRIPTION = (
    "Project to act in. Omit to use the project this connection was authorized for."
)

_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)


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


def is_multi_project(spec: dict[str, Any]) -> bool:
    """Whether the API says agents on this profile may act in more than one project.

    Absent or malformed means no. Switching is the permissive answer, so it is never the
    default for a document that does not ask for it.
    """
    extension = spec.get(MCP_EXTENSION_KEY)

    if not isinstance(extension, dict):
        return False

    return extension.get("multiProject") is True


def _declares_project_parameter(container: Any) -> bool:
    parameters = (container or {}).get("parameters")

    if not isinstance(parameters, list):
        return False

    return any(
        isinstance(parameter, dict) and parameter.get("name") == PROJECT_PARAMETER
        for parameter in parameters
    )


def inject_project_parameter(spec: dict[str, Any]) -> dict[str, Any]:
    """Give every operation an optional `project_id`, so an agent can name where to act.

    The value never reaches the API: the authorizing transport removes the header and uses
    it to choose which token to mint, and the API still takes the project from that token's
    claim. Returns a new document rather than editing the one it was handed.
    """
    paths: dict[str, Any] = {}

    for path, operations in (spec.get("paths") or {}).items():
        if not isinstance(operations, dict):
            paths[path] = operations
            continue

        if _declares_project_parameter(operations):
            raise ConfigError(
                f"{path} already declares a {PROJECT_PARAMETER!r} parameter; "
                "the MCP server cannot add its own without renaming one of them"
            )

        rebuilt: dict[str, Any] = {}

        for method, operation in operations.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                rebuilt[method] = operation
                continue

            if _declares_project_parameter(operation):
                raise ConfigError(
                    f"{method.upper()} {path} already declares a "
                    f"{PROJECT_PARAMETER!r} parameter; the MCP server cannot add its own "
                    "without renaming one of them"
                )

            rebuilt[method] = {
                **operation,
                "parameters": [
                    *operation.get("parameters", []),
                    {
                        "name": PROJECT_PARAMETER,
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": PROJECT_PARAMETER_DESCRIPTION,
                    },
                ],
            }

        paths[path] = rebuilt

    logger.info("Offered project selection on %d paths", len(paths))

    return {**spec, "paths": paths}
