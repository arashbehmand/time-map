"""
Logging with secret redaction.

A record factory redacts every log record when it is created, whichever handler or
formatter later writes it out: the message, its args and any traceback text.
"""
from __future__ import annotations

import logging
import logging.config
import re
import traceback
from collections.abc import Iterable

_PATTERNS = [
    re.compile(r"access_token=[^&\s\"'<>]+"),
    re.compile(r"\b[ps]k\.[A-Za-z0-9._-]{20,}"),
]
_secrets: tuple[str, ...] = ()
_installed = False


def set_secrets(values: Iterable[str]) -> None:
    global _secrets
    # Longest first so a token that contains another is fully removed.
    _secrets = tuple(sorted({v for v in values if v and len(v) >= 8}, key=len, reverse=True))


def redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: "access_token=[REDACTED]" if m.group(0).startswith("access_token")
                       else "[REDACTED]", text)
    return text


_PLAIN = (int, float, bool, type(None))


def _redact_arg(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, _PLAIN):
        return value
    return redact(str(value))


def _redact_record(record: logging.LogRecord) -> None:
    if record.name == "uvicorn.access" and isinstance(record.args, tuple):
        # uvicorn's access formatter unpacks record.args itself, so keep their shape.
        record.msg = redact(str(record.msg))
        record.args = tuple(_redact_arg(a) for a in record.args)
    else:
        # Merge args into the message first, so a secret split across msg and args is caught.
        try:
            message = record.getMessage()
        except Exception:  # a broken format string; keep the template
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def install_redaction() -> None:
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        _redact_record(record)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


class _DropProxyAccess(logging.Filter):
    """Tile and glyph paths show where a visitor is looking; keep them out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        path = str(args[2]) if len(args) > 2 else record.getMessage()
        return not (path.startswith("/tiles/") or path.startswith("/glyphs/")
                    or " /tiles/" in path or " /glyphs/" in path)


def configure_logging() -> None:
    install_redaction()
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "plain": {
                "format": "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
                "datefmt": "%H:%M:%S",
            },
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        },
        "root": {"handlers": ["console"], "level": "INFO"},
        "loggers": {
            # httpx logs every request URL (with the token) at INFO.
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
        },
    })
    logging.getLogger("uvicorn.access").addFilter(_DropProxyAccess())
