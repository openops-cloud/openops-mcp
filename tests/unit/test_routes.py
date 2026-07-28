import json
from pathlib import Path

import pytest

from openops_mcp.config import ConfigError
from openops_mcp.routes import load_routes

VALID_YAML = """
routes:
  - path: /mcp/flows/
    methods: [get, post]
  - path: /mcp/flows/{id}/version
    methods: [GET]
    name: get_flow_version
    description: Read one version of a flow
"""


def write(tmp_path: Path, content: str, name: str = "routes.yaml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_reads_paths_methods_and_overrides(tmp_path: Path) -> None:
    routes = load_routes(write(tmp_path, VALID_YAML))

    assert [r.path for r in routes] == ["/mcp/flows/", "/mcp/flows/{id}/version"]
    assert routes[0].methods == frozenset({"get", "post"})
    assert routes[0].name is None
    assert routes[1].name == "get_flow_version"
    assert routes[1].description == "Read one version of a flow"


def test_normalises_method_case(tmp_path: Path) -> None:
    routes = load_routes(write(tmp_path, VALID_YAML))

    assert routes[1].methods == frozenset({"get"})


def test_reads_json_as_well_as_yaml(tmp_path: Path) -> None:
    document = json.dumps({"routes": [{"path": "/v1/flow-runs/", "methods": ["get"]}]})

    routes = load_routes(write(tmp_path, document, "routes.json"))

    assert routes[0].path == "/v1/flow-runs/"


def test_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_routes(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("routes: []", "non-empty list"),
        ("routes: {}", "non-empty list"),
        ("something_else: 1", "'routes' key"),
        ("[]", "'routes' key"),
        ("just a string", "'routes' key"),
        ("routes:\n  - /mcp/flows/", "must be a mapping"),
        ("routes:\n  - methods: [get]", "starting with '/'"),
        ("routes:\n  - path: mcp/flows/\n    methods: [get]", "starting with '/'"),
        ("routes:\n  - path: /mcp/flows/", "non-empty 'methods'"),
        ("routes:\n  - path: /mcp/flows/\n    methods: []", "non-empty 'methods'"),
        ("routes:\n  - path: /mcp/flows/\n    methods: [fetch]", "unsupported method"),
        ("routes:\n  - path: /a\n    methods: [get]\n    name: ''", "not a non-empty string"),
    ],
)
def test_rejects_a_malformed_entry(tmp_path: Path, content: str, expected: str) -> None:
    with pytest.raises(ConfigError, match=expected):
        load_routes(write(tmp_path, content))


def test_reports_unparseable_content(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="could not be parsed"):
        load_routes(write(tmp_path, "routes:\n  - path: [unclosed\n"))


def test_rejects_the_same_operation_listed_twice(tmp_path: Path) -> None:
    duplicated = """
routes:
  - path: /mcp/flows/
    methods: [get, post]
  - path: /mcp/flows/
    methods: [get]
"""

    with pytest.raises(ConfigError, match="listed twice"):
        load_routes(write(tmp_path, duplicated))


def test_allows_the_same_path_with_disjoint_methods(tmp_path: Path) -> None:
    # Splitting one path across entries is reasonable, for instance to rename only one
    # of its operations.
    split = """
routes:
  - path: /mcp/flows/
    methods: [get]
    name: list_flows
  - path: /mcp/flows/
    methods: [post]
    name: create_flow
"""

    routes = load_routes(write(tmp_path, split))

    assert [sorted(r.methods) for r in routes] == [["get"], ["post"]]
    assert [r.name for r in routes] == ["list_flows", "create_flow"]
