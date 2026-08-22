import logging
import sys

from openops_mcp.logging_config import setup_logging


def test_logs_to_stderr_so_stdio_stays_a_clean_protocol_channel() -> None:
    """stdout is the MCP protocol on the stdio transport; a log line there breaks it."""
    logger = setup_logging()

    streams = [
        handler.stream
        for handler in logger.handlers
        if isinstance(handler, logging.StreamHandler)
    ]

    # pytest attaches its own capture handler, so this asserts what matters rather than
    # that every handler is ours.
    assert any(stream is sys.stderr for stream in streams), "expected a stderr handler"
    assert not any(stream is sys.stdout for stream in streams)
