"""Bounded, aggregate deployment metrics without user identifiers."""

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, time
from uuid import uuid4

from starlette.datastructures import MutableHeaders

from .state import write_json

METRICS = Path("/metrics")
def timestamp():
    return datetime.now(timezone.utc).isoformat()


def event(service, code, *, level="INFO", **data):
    print(json.dumps({"timestamp": timestamp(), "service": service, "severity": level, "code": code, **data}),
          flush=True)


def snapshot(name, data):
    try:
        write_json(METRICS / f"{name}.json", data)
    except OSError:
        event(name, "metrics_storage_error", level="ERROR")


def last_snapshot(name):
    try:
        return json.loads((METRICS / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def worker_metric_name():
    return "worker-" + os.uname().nodename


class SafeFormatter(logging.Formatter):
    def format(self, record):
        service = "worker" if "upload_worker" in record.name or os.getenv("SPI_ROLE") == "worker" else "api"
        # Application warnings use fixed formats and sanitized arguments. Suppress
        # exception text and upstream Uvicorn protocol/request details.
        message = (record.getMessage() if record.name.startswith("spi.") or service == "worker"
                   else record.name.replace(".", "_"))
        return json.dumps({"timestamp": timestamp(), "service": service, "severity": record.levelname,
                           "code": message})


class RequestMetrics:
    def __init__(self, app):
        self.app = app
        self.instance = uuid4().hex
        self.totals = Counter()
        self.last_write = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/__ops/ready":
            return await self.app(scope, receive, send)
        start, request_id, status = monotonic(), uuid4().hex, 500

        async def send_headers(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        interrupted = False
        try:
            await self.app(scope, receive, send_headers)
        except Exception:
            interrupted = True
            raise
        finally:
            elapsed = round((monotonic() - start) * 1000, 2)
            path = scope["path"]
            self.totals.update({"requests_total": 1, "request_latency_ms_sum": elapsed})
            if status >= 400:
                self.totals[f"http_{status // 100}xx"] += 1
                if path.startswith("/auth/") and status in (401, 403):
                    self.totals["authentication_failures"] += 1
                    if path == "/auth/login":
                        self.totals["login_failures"] += 1
                if status == 429:
                    self.totals["rate_limit_hits"] += 1
                if path == "/uploads" and scope["method"] == "POST":
                    self.totals["upload_failures"] += 1
                if "/export/" in path:
                    self.totals["export_failures"] += 1
            elif interrupted and "/export/" in path:
                self.totals["export_failures"] += 1
            if monotonic() - self.last_write >= 1:
                snapshot("api", {"sample_time": time(), "instance": self.instance, **self.totals})
                self.last_write = monotonic()
            event("api", "response_interrupted" if interrupted else "request",
                  request_id=request_id, status=status, duration_ms=elapsed)
