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

    logger.debug(f"[OPENOPS PYTHON] Auth token found: {auth_token[:5]}...")
    return {
        'Authorization': f'Bearer {auth_token}',
    }

def load_openapi_schema():
    schema_path = os.getenv('[OPENOPS PYTHON] OPENAPI_SCHEMA_PATH')
    if not schema_path:
        logger.error("OPENAPI_SCHEMA_PATH environment variable is not set")
        sys.exit(1)

    logger.debug(f"Loading OpenAPI schema from: {schema_path}")
    try:
        with open(schema_path, 'r') as f:
            schema_json = f.read()
            logger.debug(f"Successfully read schema file, size: {len(schema_json)} bytes")
            return json.loads(schema_json)
    except FileNotFoundError as e:
        logger.error(f"OpenAPI schema file not found at {schema_path}: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OpenAPI schema: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error loading OpenAPI schema: {e}", exc_info=True)
        sys.exit(1)

def get_base_url():
    base_url = os.getenv('API_BASE_URL')
    if not base_url:
        logger.error("API_BASE_URL environment variable is not set")
        sys.exit(1)
    logger.debug(f"Using API base URL: {base_url}")
    return base_url

def main():
    logger.info("[OPENOPS PYTHON] Starting OpenOps MCP client initialization")
    load_dotenv()
    logger.debug("[OPENOPS PYTHON] Environment variables loaded")

    auth_headers = get_auth_headers()
    logger.debug("[OPENOPS PYTHON] Auth headers configured")

   # openapi_spec = load_openapi_schema()
    logger.debug("OpenAPI schema loaded successfully")

    base_url = get_base_url()
    logger.debug("Base URL configured")

    logger.info("Creating HTTP client")
    client = httpx.AsyncClient(
        base_url=base_url,
        headers=auth_headers,
        timeout=30.0
    )
    logger.debug("HTTP client created successfully")

    try:
        logger.info("Initializing FastMCP client")
        logger.debug(f"OpenAPI spec keys: {list(openapi_spec.keys())}")
    except Exception as e:
        logger.error(f"Failed to create OpenOps MCP client: {str(e)}", exc_info=True)
        logger.error(f"Error type: {type(e).__name__}")
        logger.error(f"Error details: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
