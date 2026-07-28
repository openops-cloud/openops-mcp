import pytest

from openops_mcp.config import ConfigError, HttpSettings, StdioSettings, load_settings

STDIO_ENV = {
    "MCP_TRANSPORT": "stdio",
    "OPENOPS_API_URL": "http://localhost:3000",
    "OPENOPS_MCP_ROUTES": "/etc/openops/routes.yaml",
    "AUTH_TOKEN": "a-service-token",
}

HTTP_ENV = {
    "MCP_TRANSPORT": "http",
    "OPENOPS_API_URL": "https://app.example.com/api",
    "OPENOPS_MCP_ROUTES": "/etc/openops/routes.yaml",
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

    def test_defaults_the_openapi_url_from_the_api_url(self) -> None:
        assert load_settings(STDIO_ENV).common.openapi_url == (
            "http://localhost:3000/v1/openapi/json"
        )

    def test_honours_an_explicit_openapi_url(self) -> None:
        settings = load_settings(
            {**STDIO_ENV, "OPENOPS_API_OPENAPI_URL": "http://elsewhere/spec.json"}
        )

        assert settings.common.openapi_url == "http://elsewhere/spec.json"

    def test_defaults_to_stdio_when_no_transport_is_set(self) -> None:
        env = {k: v for k, v in STDIO_ENV.items() if k != "MCP_TRANSPORT"}

        assert isinstance(load_settings(env), StdioSettings)

    @pytest.mark.parametrize("missing", ["OPENOPS_API_URL", "OPENOPS_MCP_ROUTES", "AUTH_TOKEN"])
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
            "OPENOPS_MCP_ROUTES",
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
