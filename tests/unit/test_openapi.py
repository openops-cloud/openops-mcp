import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.server.providers.openapi import MCPType

from openops_mcp.config import ConfigError
from openops_mcp.openapi import (
    build_route_maps,
    fetch_spec,
    inject_project_parameter,
    is_multi_project,
    read_spec,
)

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "OpenOps", "version": "0.0.0"},
    "x-openops-mcp": {"multiProject": True},
    "paths": {
        "/mcp/flows/": {"get": {"operationId": "listFlows"}},
    },
}


class TestReadSpec:
    def test_reads_a_local_document(self, tmp_path: Path) -> None:
        path = tmp_path / "openapi-schema.json"
        path.write_text(json.dumps(SPEC), encoding="utf-8")

        assert read_spec(str(path)) == SPEC

    def test_reports_a_missing_file_by_name(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "absent.json")

        with pytest.raises(ConfigError, match=r"absent\.json"):
            read_spec(missing)

    def test_rejects_a_document_without_paths(self, tmp_path: Path) -> None:
        path = tmp_path / "openapi-schema.json"
        path.write_text(json.dumps({"openapi": "3.1.0"}), encoding="utf-8")

        with pytest.raises(ConfigError, match="no OpenAPI 'paths'"):
            read_spec(str(path))


class TestRouteMaps:
    def test_turns_every_remaining_operation_into_a_tool(self) -> None:
        maps = build_route_maps()

        assert len(maps) == 1
        assert maps[0].mcp_type is MCPType.TOOL


class TestFetchSpec:
    async def test_reports_a_document_without_paths(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"openapi": "3.1.0"})
        )

        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ConfigError, match="no OpenAPI 'paths'"):
                await fetch_spec("http://api/v1/mcp/openapi.json", client)


class TestMultiProject:
    def test_reads_the_capability_the_api_declared(self) -> None:
        assert is_multi_project(SPEC) is True

    def test_defaults_to_single_project(self) -> None:
        # An older API, or the community edition: absence must never enable switching.
        assert is_multi_project({"paths": {}}) is False

    def test_ignores_a_malformed_extension(self) -> None:
        assert is_multi_project({"paths": {}, "x-openops-mcp": "yes"}) is False


MULTI_OP_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "paths": {
        "/mcp/flows/": {
            "get": {"operationId": "listFlows"},
            "post": {"operationId": "createFlow"},
        },
        "/mcp/runs/{id}": {
            "get": {
                "operationId": "getRun",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
            }
        },
    },
}


def project_param(operation: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (p for p in operation.get("parameters", []) if p["name"] == "project_id"), None
    )


class TestInjectProjectParameter:
    def test_gives_every_operation_an_optional_project_id_header(self) -> None:
        injected = inject_project_parameter(MULTI_OP_SPEC)

        for path, operations in injected["paths"].items():
            for method, operation in operations.items():
                param = project_param(operation)
                assert param is not None, f"{method} {path}"
                assert param["in"] == "header"
                assert param["required"] is False
                assert param["schema"] == {"type": "string"}
                assert param["description"]

    def test_keeps_the_parameters_the_operation_already_had(self) -> None:
        injected = inject_project_parameter(MULTI_OP_SPEC)
        names = [
            p["name"]
            for p in injected["paths"]["/mcp/runs/{id}"]["get"]["parameters"]
        ]

        assert names == ["id", "project_id"]

    def test_leaves_the_document_it_was_given_alone(self) -> None:
        inject_project_parameter(MULTI_OP_SPEC)

        assert "parameters" not in MULTI_OP_SPEC["paths"]["/mcp/flows/"]["get"]
        assert len(MULTI_OP_SPEC["paths"]["/mcp/runs/{id}"]["get"]["parameters"]) == 1

    def test_refuses_an_operation_that_already_names_project_id(self) -> None:
        # FastMCP would rename ours to project_id__header, handing the model a parameter
        # nobody chose. Better to say so at startup than to ship it.
        clashing = {
            "openapi": "3.1.0",
            "paths": {
                "/mcp/flows/": {
                    "get": {
                        "operationId": "listFlows",
                        "parameters": [
                            {"name": "project_id", "in": "query",
                             "schema": {"type": "string"}}
                        ],
                    }
                }
            },
        }

        with pytest.raises(ConfigError, match="GET /mcp/flows/"):
            inject_project_parameter(clashing)

    def test_refuses_a_path_level_parameter_named_project_id(self) -> None:
        clashing = {
            "openapi": "3.1.0",
            "paths": {
                "/mcp/flows/": {
                    "parameters": [
                        {"name": "project_id", "in": "query",
                         "schema": {"type": "string"}}
                    ],
                    "get": {"operationId": "listFlows"},
                }
            },
        }

        with pytest.raises(ConfigError, match="/mcp/flows/"):
            inject_project_parameter(clashing)
