"""Controlled Uvicorn server: session token, shutdown hook, redacted file logs."""
from __future__ import annotations

import logging

import uvicorn

from app.main import app
from app.paths import get_paths


class _SecretRedactionFilter(logging.Filter):
    """Replace session tokens and API keys in every log line."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = list(secrets)

    def add_secret(self, value: str) -> None:
        if value:
            self._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            if secret:
                message = message.replace(secret, "***")
        record.msg = message
        record.args = ()
        return True


def _configure_logging(session_token: str) -> None:
    paths = get_paths()
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    redactor = _SecretRedactionFilter([session_token])
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for filename in ("startup.log", "business.log"):
        handler = logging.FileHandler(
            paths.logs_dir / filename, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        for logger_name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(logger_name).addHandler(handler)


def run_server(host: str, port: int, session_token: str) -> int:
    """Run the app under a controlled Uvicorn server; returns after clean exit."""
    _configure_logging(session_token)
    config = uvicorn.Config(
        app, host=host, port=port, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    app.state.session_token = session_token
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
    return 0
