import pytest

from openops_mcp.config import ConfigError, HttpSettings, StdioSettings, load_settings

STDIO_ENV = {
    "MCP_TRANSPORT": "stdio",
    "OPENOPS_API_URL": "http://localhost:3000",
    "AUTH_TOKEN": "a-service-token",
}

HTTP_ENV = {
    "MCP_TRANSPORT": "http",
    "OPENOPS_API_URL": "https://app.example.com/api",
    "OPENOPS_MCP_ISSUER": "https://app.example.com/api",
    "OPENOPS_MCP_RESOURCE_URL": "https://app.example.com/mcp",
    "OPENOPS_MCP_CLIENT_SECRET": "s" * 32,
}


class TestStdio:
    def test_builds_settings(self) -> None:
        settings = load_settings(STDIO_ENV)

        assert isinstance(settings, StdioSettings)
        assert settings.auth_token == "a-service-token"
        assert settings.common.api_url == "http://localhost:3000"

    def test_honours_an_explicit_openapi_url(self) -> None:
        settings = load_settings(
            {**STDIO_ENV, "OPENOPS_API_OPENAPI_URL": "http://elsewhere/spec.json"}
        )

        assert settings.common.openapi_url == "http://elsewhere/spec.json"

    def test_defaults_to_stdio_when_no_transport_is_set(self) -> None:
        env = {k: v for k, v in STDIO_ENV.items() if k != "MCP_TRANSPORT"}

        assert isinstance(load_settings(env), StdioSettings)

    @pytest.mark.parametrize("missing", ["OPENOPS_API_URL", "AUTH_TOKEN"])
    def test_names_the_variable_that_is_missing(self, missing: str) -> None:
        env = {k: v for k, v in STDIO_ENV.items() if k != missing}

        with pytest.raises(ConfigError, match=missing):
            load_settings(env)

    def test_treats_whitespace_as_missing(self) -> None:
        with pytest.raises(ConfigError, match="AUTH_TOKEN"):
            load_settings({**STDIO_ENV, "AUTH_TOKEN": "   "})


class TestHttp:
    def test_builds_settings_and_derives_the_oauth_endpoints(self) -> None:
        settings = load_settings(HTTP_ENV)

        assert isinstance(settings, HttpSettings)
        assert settings.jwks_uri == "https://app.example.com/api/v1/oauth/jwks.json"
        assert settings.token_endpoint == "https://app.example.com/api/v1/oauth/token"

    def test_defaults_the_bind_address(self) -> None:
        settings = load_settings(HTTP_ENV)

        assert (settings.host, settings.port) == ("0.0.0.0", 3020)

    @pytest.mark.parametrize(
        "missing",
        [
            "OPENOPS_MCP_ISSUER",
            "OPENOPS_MCP_RESOURCE_URL",
            "OPENOPS_MCP_CLIENT_SECRET",
        ],
    )
    def test_names_the_variable_that_is_missing(self, missing: str) -> None:
        env = {k: v for k, v in HTTP_ENV.items() if k != missing}

        with pytest.raises(ConfigError, match=missing):
            load_settings(env)

    def test_does_not_require_an_auth_token(self) -> None:
        # Each request brings its own credential, so there is nothing static to set.
        assert isinstance(load_settings(HTTP_ENV), HttpSettings)

    @pytest.mark.parametrize(
        "resource_url",
        [
            "https://app.example.com/api",
            "https://app.example.com/api/",
            "https://APP.example.com/api",
        ],
    )
    def test_refuses_a_resource_url_that_collapses_into_the_issuer(self, resource_url: str) -> None:
        # Equal audiences would mean an API token satisfies this server's audience
        # check, so nothing would stop a token being forwarded.
        with pytest.raises(ConfigError, match="must differ"):
            load_settings({**HTTP_ENV, "OPENOPS_MCP_RESOURCE_URL": resource_url})

    def test_refuses_a_short_client_secret(self) -> None:
        with pytest.raises(ConfigError, match="at least 32"):
            load_settings({**HTTP_ENV, "OPENOPS_MCP_CLIENT_SECRET": "short"})

    @pytest.mark.parametrize(
        "url",
        ["/api", "not-a-url", "ftp://example.com", "https://example.com/api?x=1", "https://x/#f"],
    )
    def test_refuses_a_malformed_issuer(self, url: str) -> None:
        with pytest.raises(ConfigError, match="OPENOPS_MCP_ISSUER"):
            load_settings({**HTTP_ENV, "OPENOPS_MCP_ISSUER": url})

    @pytest.mark.parametrize("port", ["nope", "0", "70000"])
    def test_refuses_an_invalid_port(self, port: str) -> None:
        with pytest.raises(ConfigError, match="MCP_HTTP_PORT"):
            load_settings({**HTTP_ENV, "MCP_HTTP_PORT": port})

    def test_reads_a_valid_port(self) -> None:
        settings = load_settings({**HTTP_ENV, "MCP_HTTP_PORT": "8080"})

        assert isinstance(settings, HttpSettings)
        assert settings.port == 8080


def test_refuses_an_unknown_transport() -> None:
    with pytest.raises(ConfigError, match="MCP_TRANSPORT"):
        load_settings({**STDIO_ENV, "MCP_TRANSPORT": "grpc"})


class TestSpecSource:
    def test_defaults_to_the_profiled_endpoint(self) -> None:
        settings = load_settings(STDIO_ENV)

        assert settings.common.openapi_url == (
            "http://localhost:3000/v1/mcp/openapi.json?profile=agent"
        )
        assert settings.common.openapi_path is None

    def test_honours_a_requested_profile(self) -> None:
        settings = load_settings({**STDIO_ENV, "OPENOPS_MCP_PROFILE": "chat"})

        assert settings.common.openapi_url == (
            "http://localhost:3000/v1/mcp/openapi.json?profile=chat"
        )

    def test_rejects_an_unknown_profile(self) -> None:
        with pytest.raises(ConfigError, match="OPENOPS_MCP_PROFILE"):
            load_settings({**STDIO_ENV, "OPENOPS_MCP_PROFILE": "root"})

    def test_a_local_file_wins_over_any_url(self) -> None:
        # What the API passes when it spawns this server per chat request.
        settings = load_settings(
            {**STDIO_ENV, "OPENAPI_SCHEMA_PATH": "/tmp/openapi-schema.json"}
        )

        assert settings.common.openapi_path == "/tmp/openapi-schema.json"

    def test_starts_without_a_route_list(self) -> None:
        # OPENOPS_MCP_ROUTES is gone: the API decides which operations become tools.
        assert load_settings(STDIO_ENV).common.api_url == "http://localhost:3000"


class TestTransportSecurity:
    @pytest.mark.parametrize(
        "name", ["OPENOPS_MCP_ISSUER", "OPENOPS_MCP_RESOURCE_URL"]
    )
    def test_refuses_cleartext_to_a_remote_host(self, name: str) -> None:
        # The client secret travels to the issuer as HTTP Basic, and the resource URL is
        # advertised to clients. The API refuses this configuration too.
        with pytest.raises(ConfigError, match="https"):
            load_settings({**HTTP_ENV, name: "http://app.example.com/elsewhere"})

    @pytest.mark.parametrize(
        "issuer",
        [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://[::1]:3000",
        ],
    )
    def test_allows_cleartext_to_loopback_for_local_development(self, issuer: str) -> None:
        settings = load_settings({**HTTP_ENV, "OPENOPS_MCP_ISSUER": issuer})

        assert isinstance(settings, HttpSettings)
        assert settings.issuer == issuer

    def test_still_allows_a_cleartext_api_url(self) -> None:
        # Tool calls are pod-to-pod inside a cluster; only the OAuth endpoints are public.
        settings = load_settings({**HTTP_ENV, "OPENOPS_API_URL": "http://openops-api:3000"})

        assert settings.common.api_url == "http://openops-api:3000"
