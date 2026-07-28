from typing import Any

import httpx
import pytest
from fastmcp.server.providers.openapi import MCPType

from openops_mcp.config import ConfigError
from openops_mcp.openapi import (
    assert_all_matched,
    build_mcp_names,
    build_route_maps,
    fetch_spec,
    prune_spec,
)
from openops_mcp.routes import RouteSpec

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "OpenOps", "version": "0.0.0"},
    "paths": {
        "/mcp/flows/": {
            "get": {"operationId": "listFlows"},
            "post": {"operationId": "createFlow"},
            "delete": {"operationId": "deleteFlow"},
        },
        "/mcp/flows/{id}/version": {"get": {"operationId": "getFlowVersion"}},
        "/v1/users/": {"get": {"operationId": "listUsers"}},
    },
}


def routes(*specs: tuple[str, list[str]]) -> list[RouteSpec]:
    return [RouteSpec(path=path, methods=frozenset(methods)) for path, methods in specs]


class TestPruneSpec:
    def test_keeps_only_the_listed_operations(self) -> None:
        pruned, _ = prune_spec(SPEC, routes(("/mcp/flows/", ["get"])))

        assert pruned["paths"] == {"/mcp/flows/": {"get": {"operationId": "listFlows"}}}

    def test_drops_an_unlisted_method_on_a_listed_path(self) -> None:
        # DELETE is in the document but not the allow-list, so it must not survive.
        pruned, _ = prune_spec(SPEC, routes(("/mcp/flows/", ["get", "post"])))

        assert set(pruned["paths"]["/mcp/flows/"]) == {"get", "post"}

    def test_drops_paths_that_are_not_listed(self) -> None:
        pruned, _ = prune_spec(SPEC, routes(("/mcp/flows/", ["get"])))

        assert "/v1/users/" not in pruned["paths"]

    def test_preserves_the_rest_of_the_document(self) -> None:
        pruned, _ = prune_spec(SPEC, routes(("/mcp/flows/", ["get"])))

        assert pruned["openapi"] == "3.1.0"
        assert pruned["info"]["title"] == "OpenOps"

    def test_does_not_mutate_the_original(self) -> None:
        prune_spec(SPEC, routes(("/mcp/flows/", ["get"])))

        assert set(SPEC["paths"]["/mcp/flows/"]) == {"get", "post", "delete"}

    def test_reports_a_path_the_api_does_not_expose(self) -> None:
        _, unmatched = prune_spec(SPEC, routes(("/mcp/typo/", ["get"])))

        assert [r.path for r in unmatched] == ["/mcp/typo/"]

    def test_reports_a_method_the_api_does_not_expose(self) -> None:
        _, unmatched = prune_spec(SPEC, routes(("/mcp/flows/{id}/version", ["post"])))

        assert [r.path for r in unmatched] == ["/mcp/flows/{id}/version"]

    def test_reports_nothing_when_every_entry_matches(self) -> None:
        _, unmatched = prune_spec(
            SPEC, routes(("/mcp/flows/", ["get", "post"]), ("/v1/users/", ["get"]))
        )

        assert unmatched == []

    def test_an_entry_matching_one_of_its_methods_is_not_reported(self) -> None:
        # Partial matches are permitted: the API may not implement every verb listed.
        _, unmatched = prune_spec(SPEC, routes(("/mcp/flows/", ["get", "put"])))

        assert unmatched == []


class TestAssertAllMatched:
    def test_passes_when_nothing_is_unmatched(self) -> None:
        assert_all_matched([], "routes.yaml")

    def test_names_the_file_and_the_offending_operations(self) -> None:
        unmatched = routes(("/mcp/typo/", ["get", "post"]))

        with pytest.raises(ConfigError) as raised:
            assert_all_matched(unmatched, "routes.yaml")

        assert "routes.yaml" in str(raised.value)
        assert "/mcp/typo/" in str(raised.value)
        assert "GET|POST" in str(raised.value)


class TestBuildRouteMaps:
    def test_maps_each_allowed_operation_to_a_tool(self) -> None:
        maps = build_route_maps(routes(("/mcp/flows/", ["get", "post"])))

        assert maps[0].mcp_type is MCPType.TOOL
        assert maps[0].methods == ["GET", "POST"]

    def test_ends_with_a_catch_all_exclude(self) -> None:
        # FastMCP turns every remaining route into a tool by default, so this is what
        # stops a pruning mistake quietly exposing an operation.
        maps = build_route_maps(routes(("/mcp/flows/", ["get"])))

        assert maps[-1].mcp_type is MCPType.EXCLUDE
        assert maps[-1].pattern == r".*"

    def test_anchors_the_pattern_so_a_prefix_does_not_match(self) -> None:
        import re

        pattern = build_route_maps(routes(("/mcp/flows/", ["get"])))[0].pattern

        assert re.match(pattern, "/mcp/flows/")
        assert not re.match(pattern, "/mcp/flows/extra")

    def test_keeps_openapi_parameters_matchable(self) -> None:
        import re

        pattern = build_route_maps(routes(("/mcp/flows/{id}/version", ["get"])))[0].pattern

        assert re.match(pattern, "/mcp/flows/{id}/version")


class TestBuildMcpNames:
    def test_is_empty_without_overrides(self) -> None:
        assert build_mcp_names(SPEC, routes(("/mcp/flows/", ["get"]))) == {}

    def test_maps_each_operation_id_on_the_path_to_the_override(self) -> None:
        names = build_mcp_names(
            SPEC,
            [RouteSpec(path="/mcp/flows/", methods=frozenset({"get"}), name="list_flows")],
        )

        assert names == {
            "listFlows": "list_flows",
            "createFlow": "list_flows",
            "deleteFlow": "list_flows",
        }


class TestFetchSpec:
    async def test_returns_the_document(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=SPEC))

        async with httpx.AsyncClient(transport=transport) as client:
            assert await fetch_spec("http://api/spec", client) == SPEC

    async def test_reports_an_http_failure(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(503))

        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ConfigError, match="could not fetch"):
                await fetch_spec("http://api/spec", client)

    async def test_reports_a_non_json_body(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, text="<html>"))

        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ConfigError, match="did not return JSON"):
                await fetch_spec("http://api/spec", client)

    async def test_reports_a_document_without_paths(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"openapi": "3.1.0"}))

        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ConfigError, match="no OpenAPI 'paths'"):
                await fetch_spec("http://api/spec", client)
