from typing import Dict, Optional
import httpx
from fastmcp import FastMCP
import json
import os
from dotenv import load_dotenv
import sys
import logging
from logging_config import setup_logging
import requests
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

def filter_allowed_routes(openapi_spec: dict) -> dict:
    if "paths" not in openapi_spec:
        raise ValueError("Invalid OpenAPI spec: missing 'paths' field")

    allowed_paths = get_allowed_paths()
    
    filtered_paths = {}
    for path, path_data in openapi_spec["paths"].items():
        if path in allowed_paths:
            allowed_methods = allowed_paths[path]
            filtered_methods = {}
            
            for method, method_data in path_data.items():
                if method.lower() in allowed_methods:
                    filtered_methods[method] = method_data
            
            if filtered_methods:
                filtered_paths[path] = filtered_methods

    filtered_spec = openapi_spec.copy()
    filtered_spec["paths"] = filtered_paths

    return filtered_spec

def get_base_url():
    base_url = os.getenv('API_BASE_URL')
    if not base_url:
        logger.error("API_BASE_URL environment variable is not set")
        sys.exit(1)
    return base_url


def get_allowed_paths() -> dict:
    return {
        '/mcp/runs/{id}': ['get'],
        '/mcp/runs/{flowVersionId}/start-test-run': ['post'],

        '/mcp/flows/': ['get', 'post'],
        '/mcp/flows/{id}/version': ['get'],
        '/mcp/flows/{flowId}/add-step': ['post'],
        '/mcp/flows/{flowId}/update-step': ['put'],
        '/mcp/flows/{flowId}/delete-step/{stepId}': ['delete'],
        '/mcp/flows/versions/{flowVersionId}/execute-step/{stepId}': ['post'],
        '/mcp/flows/{flowId}/update-trigger': ['put'],
        '/mcp/flows/{flowVersionId}/steps/{stepId}/test-output': ['get'],

        '/mcp/blocks/': ['get'],
        '/mcp/blocks/{packageScope}/{packageName}/actions': ['get'],
        '/mcp/blocks/{packageScope}/{packageName}/triggers': ['get'],
        '/mcp/blocks/{packageScope}/{packageName}/actions/{name}': ['get'],
        '/mcp/blocks/{packageScope}/{packageName}/triggers/{name}': ['get'],

        '/v1/flow-runs/': ['get'],
        '/v1/flow-runs/{id}/retry': ['post'],
        '/v1/app-connections/': ['get', 'patch'],
        '/v1/app-connections/{id}': ['get'],
        '/v1/app-connections/metadata': ['get'],
    }

def main():
    load_dotenv()

    auth_headers = get_auth_headers()
    raw_spec = load_openapi_schema()
    openapi_spec = filter_allowed_routes(raw_spec)
    base_url = get_base_url()

    client = httpx.AsyncClient(
        base_url=base_url,
        headers=auth_headers,
        timeout=30.0
    )

    route_maps = [
        RouteMap(
            methods="*",
            pattern=r"^/mcp/.*",
            mcp_type=MCPType.TOOL
        ),
        RouteMap(
            methods="*",
            pattern=r"^/v1/(flow-runs|app-connections)/.*",
            mcp_type=MCPType.TOOL
        )
    ]

    try:
        mcp = FastMCP.from_openapi(
            openapi_spec=openapi_spec,
            client=client,
            name="OpenOps API Server",
            all_routes_as_tools=False,
            default_headers=auth_headers,
            route_maps=route_maps,
        )
        mcp.run()
        return mcp
    except Exception as e:
        logger.error(f"Failed to create OpenOps MCP client: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
