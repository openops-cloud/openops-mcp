# OpenOps MCP

This is a FastMCP server implementation for OpenOps that provides a managed control plane using FastMCP and OpenAPI specifications.

## Setup

1. Create a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file with the following variables:
```bash
AUTH_TOKEN=your_auth_token
OPENAPI_SCHEMA=your_openapi_schema
API_BASE_URL=your_api_base_url
LOGZIO_TOKEN=your_logzio_token  # Optional: for Logz.io logging
```

## Key Dependencies

- `fastmcp`: FastMCP framework for building managed control planes
- `httpx`: Async HTTP client for making API requests
- `python-dotenv`: For environment variable management
- `logzio-python-handler`: For Logz.io logging integration

## Running the Server

```bash
python main.py
```

## Logging

The server supports two types of logging:

1. **Console Logging**: All logs are output to the console with DEBUG level, providing detailed information during development and debugging.

2. **Logz.io Logging**: If `LOGZIO_TOKEN` is set in the environment, logs will also be sent to Logz.io with INFO level. This is useful for production monitoring and log aggregation.

To enable Logz.io logging:
1. Get your Logz.io token from your Logz.io account
2. Add it to your `.env` file as `LOGZIO_TOKEN=your_token`

## Environment Variables

- `AUTH_TOKEN`: Authentication token for API requests
- `OPENAPI_SCHEMA`: OpenAPI schema in JSON format that defines the API structure
- `API_BASE_URL`: Base URL for the API endpoints
- `LOGZIO_TOKEN`: (Optional) Logz.io token for remote logging
- `ENVIRONMENT`: (Optional) Used for Logz.io