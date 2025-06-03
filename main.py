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
    logger.info("Retrieving authentication headers")
    auth_token = os.getenv('AUTH_TOKEN')
    if not auth_token:
        logger.error("AUTH_TOKEN environment variable is not set")
        sys.exit(1)

    logger.debug("Successfully retrieved auth token")
    return {
        'Authorization': f'Bearer {auth_token}',
    }

def load_openapi_schema():
    logger.info("Loading OpenAPI schema")
    schema_path = os.getenv('OPENAPI_SCHEMA_PATH')
    if not schema_path:
        logger.error("OPENAPI_SCHEMA_PATH environment variable is not set")
        sys.exit(1)

    try:
        with open(schema_path, 'r') as f:
            logger.debug(f"Reading schema from {schema_path}")
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read or parse OpenAPI schema file: {e}")
        sys.exit(1)

    try:
        return json.loads(schema_json)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OpenAPI schema: {str(e)}")
        sys.exit(1)

def get_base_url():
    logger.info("Retrieving API base URL")
    base_url = os.getenv('API_BASE_URL')
    if not base_url:
        logger.error("API_BASE_URL environment variable is not set")
        sys.exit(1)
    logger.debug(f"Base URL retrieved: {base_url}")
    return base_url

def main():
    logger.info("Starting OpenOps MCP application")
    load_dotenv()
    logger.debug("Environment variables loaded")

    auth_headers = get_auth_headers()
    openapi_spec = load_openapi_schema()
    base_url = get_base_url()

    logger.info("Initializing HTTP client")
    client = httpx.AsyncClient(
        base_url=base_url,
        headers=auth_headers,
        timeout=30.0
    )

    try:
        logger.info("Creating FastMCP instance")
        mcp = FastMCP.from_openapi(
            openapi_spec=openapi_spec,
            client=client,
            name="OpenOps API Server",
            all_routes_as_tools=True,
            default_headers=auth_headers,
        )
        logger.info("Starting FastMCP server")
        mcp.run()
        return mcp
    except Exception as e:
        logger.error(f"Failed to create OpenOps MCP client: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
