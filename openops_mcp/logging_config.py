import logging
import logging.config
import os
import re
import sys


class LowercaseFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.levelname = record.levelname.lower()
        return super().format(record)


class HTTPErrorFilter(logging.Filter):
    """Filter to suppress 4xx HTTP errors from being sent to logz.io"""

    HTTP_4XX_PATTERN = re.compile(r"HTTP error 4\d{2}")

    def filter(self, record: logging.LogRecord) -> bool:
        # A 4xx is the caller's mistake, not an incident. Kept on the console but not
        # shipped, so alerting is not driven by clients sending bad requests.
        is_client_error = record.levelno == logging.ERROR and bool(
            self.HTTP_4XX_PATTERN.search(record.getMessage())
        )
        return not is_client_error


def _console_level() -> int:
    """INFO unless asked otherwise. DEBUG on the root logger makes every dependency
    verbose, and those lines go wherever this server's stderr goes."""
    requested = os.getenv("LOG_LEVEL", "").strip().upper()
    level = logging.getLevelName(requested) if requested else logging.INFO

    return level if isinstance(level, int) else logging.INFO


def setup_logging() -> logging.Logger:
    level = _console_level()
    logger = logging.getLogger()
    logger.setLevel(level)

    # stderr, never stdout: on the stdio transport stdout carries the MCP protocol, so a
    # single log line written there corrupts the stream and the client drops the session.
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
    LOGZIO_TOKEN = os.getenv("LOGZIO_TOKEN")

    if LOGZIO_TOKEN:
        LOGGING = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "logzioFormat": {
                    "()": LowercaseFormatter,
                    "format": (
                        '{"level": "%(levelname)s", '
                        f'"environment": "{ENVIRONMENT}", '
                        '"component": "openops mcp"}'
                    ),
                    "validate": False,
                }
            },
            "filters": {
                "http_error_filter": {
                    "()": HTTPErrorFilter,
                }
            },
            "handlers": {
                "logzio": {
                    "class": "logzio.handler.LogzioHandler",
                    "level": "INFO",
                    "formatter": "logzioFormat",
                    "token": LOGZIO_TOKEN,
                    "logzio_type": "openops-mcp",
                    "logs_drain_timeout": 5,
                    "url": "https://listener.logz.io:8071",
                    "retries_no": 4,
                    "retry_timeout": 2,
                    "filters": ["http_error_filter"],
                }
            },
            "loggers": {"": {"level": "DEBUG", "handlers": ["logzio"], "propagate": True}},
        }
        logging.config.dictConfig(LOGGING)
        logger.info("Logz.io logging configured successfully")

    return logger
