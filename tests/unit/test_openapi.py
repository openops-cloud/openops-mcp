import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.server.providers.openapi import MCPType

from openops_mcp.config import ConfigError
from openops_mcp.openapi import build_route_maps, fetch_spec, read_spec

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
