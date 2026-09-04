"""stderr-only structured logging.

The rule for a stdio MCP server: fd 1 is a framed JSON-RPC channel and nothing
else. Every diagnostic in this process is routed here, and this module writes
to ``sys.stderr`` exclusively.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

LOGGER_NAME = "mcp_refund_server"


class JsonStderrFormatter(logging.Formatter):
    """One JSON object per line, so the log is greppable and machine-readable."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed as ``extra={"fields": {...}}`` is merged in.
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> logging.Logger:
    """Point the root logger at stderr and return the server's logger.

    Configuring the *root* logger matters: a dependency that calls
    ``logging.info(...)`` would otherwise fall back to a handler of its own.
    """
    level = os.environ.get("MCP_LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonStderrFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    return logging.getLogger(LOGGER_NAME)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Log with structured fields: ``log(logger, logging.INFO, "hi", a=1)``."""
    logger.log(level, msg, extra={"fields": fields})
