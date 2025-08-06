from typing import Dict, Optional
import httpx
from fastmcp import FastMCP
import json
import os
from dotenv import load_dotenv
import sys
from logging_config import setup_logging
from fastmcp.server.openapi import RouteMap, MCPType

logger = setup_logging()

def get_auth_headers():
    auth_token = os.getenv('AUTH_TOKEN')
    if not auth_token:
        logger.error("AUTH_TOKEN environment variable is not set")
        sys.exit(1)

    return {
        'Authorization': f'Bearer {auth_token}',
    }

def load_openapi_schema():
    schema_url = os.getenv('OPENAPI_SCHEMA_URL', 'http://localhost:3000/v1/openapi/json')

    try:
        response = requests.get(schema_url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch OpenAPI schema from {schema_url}: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OpenAPI schema from {schema_url}: {e}")
        sys.exit(1)

def filter_v2_routes(openapi_spec: dict) -> dict:
    if "paths" not in openapi_spec:
        raise ValueError("Invalid OpenAPI spec: missing 'paths' field")

    v2_paths = {path: data for path, data in openapi_spec["paths"].items() if path.startswith("/v2")}

    # Create a shallow copy of the spec and replace paths
    filtered_spec = openapi_spec.copy()
    filtered_spec["paths"] = v2_paths

    return filtered_spec

def get_base_url():
    base_url = os.getenv('API_BASE_URL')
    if not base_url:
        logger.error("API_BASE_URL environment variable is not set")
        sys.exit(1)
    return base_url

def main():
    load_dotenv()

    auth_headers = get_auth_headers()
    openapi_spec = load_openapi_schema()
    base_url = get_base_url()

    client = httpx.AsyncClient(
        base_url=base_url,
        headers=auth_headers,
        timeout=30.0
    )

    route_maps = [
        RouteMap(
            methods="*",
            pattern=r"^/v2/.*",
            mcp_type=MCPType.TOOL
        )
    ]

    try:
        mcp = FastMCP.from_openapi(
            openapi_spec=openapi_spec,
            client=client,
            name="OpenOps API Server",
            # all_routes_as_tools=True,
            all_routes_as_tools=False,
            route_maps=route_maps,
            default_headers=auth_headers,
        )
        mcp.run()
        return mcp
    except Exception as e:
        logger.error(f"Failed to create OpenOps MCP client: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
