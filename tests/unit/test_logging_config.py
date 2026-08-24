import logging
import sys

import pytest

from openops_mcp import __main__ as entrypoint
from openops_mcp.config import ConfigError
from openops_mcp.logging_config import setup_logging


def _refuse() -> None:
    raise ConfigError("stop here")


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


def test_the_env_file_is_loaded_before_logging_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise LOGZIO_TOKEN in .env is invisible and shipping is never enabled."""
    calls: list[str] = []

    monkeypatch.setattr(entrypoint, "load_dotenv", lambda: calls.append("dotenv"))
    monkeypatch.setattr(entrypoint, "setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(entrypoint, "load_settings", _refuse)

    with pytest.raises(SystemExit):
        entrypoint.main()

    assert calls == ["dotenv", "logging"]
