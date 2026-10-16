import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        extra = getattr(record, "ctx", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, as_json: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    # Replace console handlers only; keep exporters such as the Application Insights handler.
    root.handlers[:] = [h for h in root.handlers if type(h) is not logging.StreamHandler] + [handler]
    root.setLevel(level)
    for noisy in ("botocore", "boto3", "urllib3", "aiosqlite", "azure", "azure.core.pipeline"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
