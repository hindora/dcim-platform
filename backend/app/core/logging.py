"""Structured logging.

JSON in every environment except an interactive terminal, and a redaction
processor that scrubs anything credential-shaped. Redaction is enforced here
rather than trusted to call sites, because one careless ``log.info(endpoint)``
is all it takes to put a community string in a log aggregator forever.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_SECRET_KEYS = re.compile(
    r"(password|secret|token|community|private_key|credential|authorization)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


def _redact(_logger: Any, _name: str, event_dict: dict) -> dict:
    for key in list(event_dict):
        if _SECRET_KEYS.search(key):
            event_dict[key] = _REDACTED
        elif isinstance(event_dict[key], dict):
            event_dict[key] = {
                k: (_REDACTED if _SECRET_KEYS.search(k) else v)
                for k, v in event_dict[key].items()
            }
    return event_dict


def configure_logging(level: str = "INFO", service: str = "dcim-backend",
                      json_output: bool | None = None) -> None:
    if json_output is None:
        json_output = not sys.stderr.isatty()

    renderer = (structlog.processors.JSONRenderer() if json_output
                else structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)

    logging.basicConfig(format="%(message)s", stream=sys.stderr,
                        level=getattr(logging, level.upper(), logging.INFO))
    # uvicorn's access log duplicates what the middleware already records.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str = "") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
