"""Small JSON-lines logging helper."""

import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(service: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(service)
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger
