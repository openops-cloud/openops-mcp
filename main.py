from typing import Dict, Optional
import httpx
from fastmcp import FastMCP
import json
import os
from dotenv import load_dotenv
import sys
import logging
from logging_config import setup_logging

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
    schema_json = os.getenv('OPENAPI_SCHEMA')
    if not schema_json:
        logger.error("OPENAPI_SCHEMA environment variable is not set")
        sys.exit(1)

    try:
        return json.loads(schema_json)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OpenAPI schema: {str(e)}")
        sys.exit(1)

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

    try:
        mcp = FastMCP.from_openapi(
            openapi_spec=openapi_spec,
            client=client,
            name="OpenOps API Server",
            all_routes_as_tools=True,
            default_headers=auth_headers,
        )
        mcp.run()
        return mcp
    except Exception as e:
        logger.error(f"Failed to create OpenOps MCP client: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
