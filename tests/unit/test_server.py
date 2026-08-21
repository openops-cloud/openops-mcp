from typing import Any

import httpx
from fastmcp import Client

from openops_mcp.server import build_server

# Already filtered to one profile, the way the API serves it.
SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "OpenOps", "version": "0.0.0"},
    "x-openops-mcp": {"multiProject": False},
    "paths": {
        "/mcp/flows/": {
            "get": {
                "operationId": "listFlows",
                "description": "Every workflow in the project",
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "createFlow",
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/mcp/runs/{id}": {
            "get": {
                "operationId": "getRun",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def api_client() -> httpx.AsyncClient:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []}))
    return httpx.AsyncClient(transport=transport, base_url="http://api")


async def test_exposes_a_tool_for_every_operation_in_the_document() -> None:
    server = build_server(spec=SPEC, client=api_client())

    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == {"listFlows", "createFlow", "getRun"}


async def test_serves_no_tools_for_an_empty_document() -> None:
    # The API is the allow-list now, so an empty document must yield an empty surface
    # rather than falling back to every route FastMCP can find.
    server = build_server(spec={**SPEC, "paths": {}}, client=api_client())

    async with Client(server) as client:
        assert await client.list_tools() == []


async def test_takes_tool_descriptions_from_the_document() -> None:
    server = build_server(spec=SPEC, client=api_client())

    async with Client(server) as client:
        tools = {tool.name: tool.description for tool in await client.list_tools()}

    assert tools["listFlows"] == "Every workflow in the project"


async def test_a_tool_call_reaches_the_api() -> None:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(record),
        base_url="http://api",
        headers={"Authorization": "Bearer static-token"},
    )
    server = build_server(spec=SPEC, client=client)

    async with Client(server) as mcp_client:
        await mcp_client.call_tool("listFlows", {})

    assert [request.url.path for request in seen] == ["/mcp/flows/"]
    assert seen[0].headers["authorization"] == "Bearer static-token"
