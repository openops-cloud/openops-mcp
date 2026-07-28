from typing import Any

import httpx
import pytest
from fastmcp import Client

from openops_mcp.openapi import prune_spec
from openops_mcp.routes import RouteSpec
from openops_mcp.server import build_server

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "OpenOps", "version": "0.0.0"},
    "paths": {
        "/mcp/flows/": {
            "get": {
                "operationId": "listFlows",
                "summary": "List flows",
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "createFlow",
                "summary": "Create a flow",
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {
                "operationId": "deleteFlow",
                "summary": "Delete a flow",
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/v1/users/": {
            "get": {
                "operationId": "listUsers",
                "summary": "List users",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def api_client() -> httpx.AsyncClient:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []}))
    return httpx.AsyncClient(transport=transport, base_url="http://api")


async def tool_names(routes: list[RouteSpec]) -> set[str]:
    pruned, _ = prune_spec(SPEC, routes)
    server = build_server(spec=pruned, routes=routes, client=api_client())

    async with Client(server) as client:
        return {tool.name for tool in await client.list_tools()}


async def test_exposes_a_tool_per_allowed_operation() -> None:
    names = await tool_names([RouteSpec(path="/mcp/flows/", methods=frozenset({"get", "post"}))])

    assert len(names) == 2


async def test_does_not_expose_an_unlisted_method_on_a_listed_path() -> None:
    names = await tool_names([RouteSpec(path="/mcp/flows/", methods=frozenset({"get"}))])

    assert len(names) == 1
    assert not any("delete" in name.lower() for name in names)


async def test_does_not_expose_an_unlisted_path() -> None:
    names = await tool_names([RouteSpec(path="/mcp/flows/", methods=frozenset({"get"}))])

    assert not any("user" in name.lower() for name in names)


async def test_honours_a_tool_name_override() -> None:
    names = await tool_names(
        [RouteSpec(path="/mcp/flows/", methods=frozenset({"get"}), name="list_flows")]
    )

    assert names == {"list_flows"}


async def test_honours_a_description_override() -> None:
    routes = [
        RouteSpec(
            path="/mcp/flows/",
            methods=frozenset({"get"}),
            description="Every flow in the project",
        )
    ]
    pruned, _ = prune_spec(SPEC, routes)
    server = build_server(spec=pruned, routes=routes, client=api_client())

    async with Client(server) as client:
        tools = await client.list_tools()

    assert tools[0].description == "Every flow in the project"


async def test_serves_no_tools_when_nothing_survives_pruning() -> None:
    # Belt and braces: even handed an empty document, the catch-all exclude means the
    # server exposes nothing rather than defaulting to every route.
    server = build_server(
        spec={**SPEC, "paths": {}},
        routes=[RouteSpec(path="/mcp/flows/", methods=frozenset({"get"}))],
        client=api_client(),
    )

    async with Client(server) as client:
        assert await client.list_tools() == []


async def test_a_tool_call_reaches_the_api() -> None:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    routes = [RouteSpec(path="/mcp/flows/", methods=frozenset({"get"}), name="list_flows")]
    pruned, _ = prune_spec(SPEC, routes)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(record),
        base_url="http://api",
        headers={"Authorization": "Bearer static-token"},
    )
    server = build_server(spec=pruned, routes=routes, client=client)

    async with Client(server) as mcp_client:
        await mcp_client.call_tool("list_flows", {})

    assert [request.url.path for request in seen] == ["/mcp/flows/"]
    assert seen[0].headers["authorization"] == "Bearer static-token"


@pytest.mark.parametrize("path", ["/mcp/flows/", "/v1/users/"])
async def test_every_listed_path_yields_at_least_one_tool(path: str) -> None:
    names = await tool_names([RouteSpec(path=path, methods=frozenset({"get"}))])

    assert len(names) == 1
