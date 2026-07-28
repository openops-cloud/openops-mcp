"""Environment into validated settings.

Everything is read and checked once, at startup, so a misconfiguration names the
variable at fault instead of surfacing later as a confusing request failure.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

Transport = Literal["stdio", "http"]

DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 3020
DEFAULT_OPENAPI_PATH = "/v1/openapi/json"

# The resource server authenticates to the authorization server as this client. It
# must match RS_CLIENT_ID in the OpenOps API.
CLIENT_ID = "openops-mcp-rs"

# Anything shorter is guessable for a credential that never rotates on its own.
MIN_CLIENT_SECRET_LENGTH = 32


class ConfigError(Exception):
    """A configuration fault that must stop startup."""


@dataclass(frozen=True)
class CommonSettings:
    api_url: str
    openapi_url: str
    routes_file: str


@dataclass(frozen=True)
class StdioSettings:
    common: CommonSettings
    auth_token: str
    transport: Transport = "stdio"


@dataclass(frozen=True)
class HttpSettings:
    common: CommonSettings
    issuer: str
    resource_url: str
    client_secret: str
    host: str = DEFAULT_HTTP_HOST
    port: int = DEFAULT_HTTP_PORT
    transport: Transport = "http"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/v1/oauth/jwks.json"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/v1/oauth/token"


Settings = StdioSettings | HttpSettings


def _require(name: str, env: dict[str, str]) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _require_url(name: str, env: dict[str, str]) -> str:
    value = _require(name, env)
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL, got {value!r}")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must not contain a query string or fragment")

    return value.rstrip("/")


def _read_port(env: dict[str, str]) -> int:
    raw = env.get("MCP_HTTP_PORT", "").strip()
    if not raw:
        return DEFAULT_HTTP_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigError(f"MCP_HTTP_PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"MCP_HTTP_PORT must be between 1 and 65535, got {port}")
    return port


def _read_transport(env: dict[str, str]) -> Transport:
    value = env.get("MCP_TRANSPORT", "stdio").strip().lower()
    if value not in ("stdio", "http"):
        raise ConfigError(f"MCP_TRANSPORT must be 'stdio' or 'http', got {value!r}")
    return value  # type: ignore[return-value]


def _load_common(env: dict[str, str]) -> CommonSettings:
    # The API has historically spawned this server with API_BASE_URL and
    # OPENAPI_SCHEMA_URL. Those are accepted as fallbacks so the two sides can be
    # rolled out independently; they are pure renames with no change in meaning.
    _accept_legacy(env, "OPENOPS_API_URL", "API_BASE_URL")
    _accept_legacy(env, "OPENOPS_API_OPENAPI_URL", "OPENAPI_SCHEMA_URL")

    api_url = _require_url("OPENOPS_API_URL", env)
    openapi_url = env.get("OPENOPS_API_OPENAPI_URL", "").strip()

    return CommonSettings(
        api_url=api_url,
        openapi_url=openapi_url.rstrip("/") if openapi_url else f"{api_url}{DEFAULT_OPENAPI_PATH}",
        routes_file=_require("OPENOPS_MCP_ROUTES", env),
    )


def _accept_legacy(env: dict[str, str], current: str, legacy: str) -> None:
    if not env.get(current, "").strip() and env.get(legacy, "").strip():
        logger.warning("%s is deprecated; set %s instead", legacy, current)
        env[current] = env[legacy]


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build settings for the selected transport, or raise ConfigError."""
    env = dict(os.environ if env is None else env)
    transport = _read_transport(env)
    common = _load_common(env)

    if transport == "stdio":
        return StdioSettings(common=common, auth_token=_require("AUTH_TOKEN", env))

    issuer = _require_url("OPENOPS_MCP_ISSUER", env)
    resource_url = _require_url("OPENOPS_MCP_RESOURCE_URL", env)

    # Equal audiences would mean a token minted for the API satisfies this server's
    # audience check, so forwarding it would no longer be prevented by anything. The
    # authorization server refuses to boot in the same situation.
    if _canonical(resource_url) == _canonical(issuer):
        raise ConfigError(
            "OPENOPS_MCP_RESOURCE_URL must differ from OPENOPS_MCP_ISSUER: "
            "each resource needs its own token audience"
        )

    client_secret = _require("OPENOPS_MCP_CLIENT_SECRET", env)
    if len(client_secret) < MIN_CLIENT_SECRET_LENGTH:
        raise ConfigError(
            f"OPENOPS_MCP_CLIENT_SECRET must be at least {MIN_CLIENT_SECRET_LENGTH} characters"
        )

    return HttpSettings(
        common=common,
        issuer=issuer,
        resource_url=resource_url,
        client_secret=client_secret,
        host=env.get("MCP_HTTP_HOST", DEFAULT_HTTP_HOST).strip() or DEFAULT_HTTP_HOST,
        port=_read_port(env),
    )


def _canonical(url: str) -> str:
    """Scheme and host are case-insensitive, and a trailing slash names the same URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
