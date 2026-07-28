"""The allow-list of API operations that become tools.

Supplied as a file rather than compiled in, so which operations an agent can reach
is a deployment decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, get_args

import yaml

from .config import ConfigError

# Lower-case here because that is how OpenAPI keys operations; FastMCP wants them
# upper-cased, and `openapi.build_route_maps` maps between the two.
HttpMethod = Literal["get", "post", "put", "patch", "delete", "head", "options"]

HTTP_METHODS: frozenset[str] = frozenset(get_args(HttpMethod))


@dataclass(frozen=True)
class RouteSpec:
    """One allowed API operation, optionally renamed for the agent."""

    path: str
    methods: frozenset[HttpMethod] = field(default_factory=frozenset)
    name: str | None = None
    description: str | None = None


def load_routes(path: str | Path) -> list[RouteSpec]:
    """Read and validate the allow-list, or raise ConfigError."""
    file_path = Path(path)

    if not file_path.is_file():
        raise ConfigError(f"route list not found: {file_path}")

    document = _parse(file_path)

    if not isinstance(document, dict) or "routes" not in document:
        raise ConfigError(f"{file_path}: expected a mapping with a 'routes' key")

    entries = document["routes"]
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{file_path}: 'routes' must be a non-empty list")

    routes = [_parse_entry(entry, index, file_path) for index, entry in enumerate(entries)]
    _reject_duplicates(routes, file_path)

    return routes


def _parse(file_path: Path) -> Any:
    text = file_path.read_text(encoding="utf-8")

    try:
        # YAML is a superset of JSON, so one parser handles both extensions.
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file_path}: could not be parsed as YAML or JSON: {exc}") from exc


def _parse_entry(entry: Any, index: int, file_path: Path) -> RouteSpec:
    where = f"{file_path}: routes[{index}]"

    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a mapping")

    path = entry.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ConfigError(f"{where} needs a 'path' starting with '/'")

    raw_methods = entry.get("methods")
    if not isinstance(raw_methods, list) or not raw_methods:
        raise ConfigError(f"{where} ({path}) needs a non-empty 'methods' list")

    methods: set[HttpMethod] = set()
    for method in raw_methods:
        if not isinstance(method, str) or method.lower() not in HTTP_METHODS:
            raise ConfigError(
                f"{where} ({path}) has an unsupported method {method!r}; "
                f"expected one of {sorted(HTTP_METHODS)}"
            )
        methods.add(cast(HttpMethod, method.lower()))

    return RouteSpec(
        path=path,
        methods=frozenset(methods),
        name=_optional_str(entry, "name", where),
        description=_optional_str(entry, "description", where),
    )


def _optional_str(entry: dict[str, Any], key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} has a '{key}' that is not a non-empty string")
    return value


def _reject_duplicates(routes: list[RouteSpec], file_path: Path) -> None:
    seen: set[tuple[str, str]] = set()

    for route in routes:
        for method in route.methods:
            key = (route.path, method)
            if key in seen:
                raise ConfigError(f"{file_path}: {method.upper()} {route.path} is listed twice")
            seen.add(key)


def dump_example() -> str:
    """A minimal file, used in documentation and error messages."""
    return json.dumps(
        {"routes": [{"path": "/mcp/flows/", "methods": ["get", "post"]}]},
        indent=2,
    )
