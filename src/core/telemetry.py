"""Local diagnostics: fixed metric names, numeric context, stacks without payloads."""

import asyncio
import json
import logging
import platform
import sys
import threading
import traceback
import uuid
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter

import httpx

log = logging.getLogger("recruiter.operations")

OPERATIONS = {
    "runtime",
    "codex",
    "telegram.read",
    "telegram.send",
    "telegram.notify",
    "vacancy.fetch",
    "interview.step",
    "command",
}
ERROR_EVENTS = {
    "runtime.failed",
    "runtime.background",
    "interview.failed",
    "command.failed",
}


class Telemetry:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.storage_failed = False
        self.metrics_path = directory / "metrics.json"
        self.session = uuid.uuid4().hex[:12]
        self.counters = Counter()
        self.timings: dict[str, dict] = {}
        if self.metrics_path.exists():
            try:
                saved = json.loads(self.metrics_path.read_text())
                if not isinstance(saved["counters"], dict) or not isinstance(
                    saved["timings"], dict
                ):
                    raise ValueError("Invalid metrics snapshot")
                if not all(
                    isinstance(value, int) and value >= 0
                    for value in saved["counters"].values()
                ):
                    raise ValueError("Invalid counters")
                for timing in saved["timings"].values():
                    if (
                        not isinstance(timing, dict)
                        or not isinstance(timing.get("count"), int)
                        or timing["count"] <= 0
                        or not all(
                            isinstance(timing.get(key), (int, float))
                            and timing[key] >= 0
                            for key in ("total_seconds", "max_seconds")
                        )
                    ):
                        raise ValueError("Invalid timings")
                self.counters.update(saved["counters"])
                self.timings = saved["timings"]
            except (ValueError, KeyError, TypeError):
                self.counters["diagnostics.load_failed"] += 1
        self._handler = RotatingFileHandler(
            directory / "events.jsonl",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._flush()

    def _flush(self):
        temporary = self.metrics_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {"counters": dict(self.counters), "timings": self.timings},
                    ensure_ascii=False,
                )
            )
            temporary.chmod(0o600)
            temporary.replace(self.metrics_path)
        except OSError:
            self.storage_failed = True

    def _write(self, event: dict):
        record = logging.LogRecord(
            "recruiter.diagnostics",
            logging.INFO,
            "",
            0,
            json.dumps(event, ensure_ascii=False),
            (),
            None,
        )
        self._handler.handle(record)

    @contextmanager
    def operation(self, name: str):
        if name not in OPERATIONS:
            raise ValueError("Unknown diagnostic operation")
        started = perf_counter()
        outcome = "ok"
        log.info("%s started", name)
        try:
            yield
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            elapsed = perf_counter() - started
            log.log(
                logging.ERROR if outcome == "failed" else logging.INFO,
                "%s %s duration=%.2fs",
                name,
                outcome,
                elapsed,
            )
            with self._lock:
                self.counters[f"{name}.{outcome}"] += 1
                timing = self.timings.setdefault(
                    name, {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0}
                )
                timing["count"] += 1
                timing["total_seconds"] += elapsed
                timing["max_seconds"] = max(timing["max_seconds"], elapsed)
                self._flush()

    def error(self, name: str, exc: BaseException, *, phase: str | None = None) -> str:
        if name not in ERROR_EVENTS:
            raise ValueError("Unknown diagnostic event")
        incident = uuid.uuid4().hex[:12]
        # No exception messages, source lines, locals, commands, URLs or user text.
        stack = [
            {
                "file": Path(frame.filename).name,
                "line": frame.lineno,
                "function": frame.name,
            }
            for frame in traceback.extract_tb(exc.__traceback__)
        ]
        chain = []
        cause = exc.__cause__ or exc.__context__
        visited = {id(exc)}
        while cause is not None and id(cause) not in visited:
            visited.add(id(cause))
            chain.append(type(cause).__name__)
            cause = cause.__cause__ or cause.__context__
        safe_phase = (
            phase
            if phase
            in {"idle", "clicking", "sending", "interview", "finishing", "rating"}
            else "unknown"
        )
        event = {
            "time": datetime.now(UTC).isoformat(),
            "session": self.session,
            "incident": incident,
            "event": name,
            "exception": type(exc).__name__,
            "causes": chain,
            "phase": safe_phase,
            "stack": stack,
        }
        if isinstance(exc, httpx.HTTPStatusError):
            event["http_status"] = exc.response.status_code
        code = getattr(exc, "diagnostic_code", None)
        if code in {
            "auth",
            "rate_limit",
            "access",
            "network",
            "timeout",
            "recruiter_timeout",
            "invalid_response",
            "unknown",
        }:
            event["code"] = code
        log.error(
            "%s incident=%s exception=%s phase=%s http_status=%s code=%s\n%s",
            name,
            incident,
            event["exception"],
            safe_phase,
            event.get("http_status", "-"),
            event.get("code", "-"),
            "\n".join(
                f"  {frame['file']}:{frame['line']} in {frame['function']}"
                for frame in stack
            ),
        )
        with self._lock:
            self.counters[f"errors.{name}"] += 1
            try:
                self._write(event)
            except OSError:
                self.storage_failed = True
            self._flush()
            try:
                self._handler.flush()
                self.export()
            except OSError:
                self.storage_failed = True
        return incident

    def summary(self) -> str:
        with self._lock:
            errors = sum(self.counters[f"errors.{name}"] for name in ERROR_EVENTS)
            lines = [f"Зафиксировано ошибок: {errors}."]
            if self.storage_failed:
                lines.append(
                    "Не удалось сохранить часть диагностики: проверь свободное место и доступ к state/."
                )
            for name, timing in sorted(self.timings.items()):
                mean = timing["total_seconds"] / timing["count"]
                lines.append(
                    f"{name}: {timing['count']} вызовов "
                    f"(успех {self.counters[f'{name}.ok']}, "
                    f"сбой {self.counters[f'{name}.failed']}, "
                    f"отмена {self.counters[f'{name}.cancelled']}), "
                    f"среднее {mean:.2f} с, максимум {timing['max_seconds']:.2f} с"
                )
            return "\n".join(lines)

    def export(self) -> Path:
        """An explicit allowlist report, never an archive of state or credentials."""
        with self._lock:
            events = []
            files = [self.directory / f"events.jsonl.{i}" for i in (3, 2, 1)] + [
                self.directory / "events.jsonl"
            ]
            for file in files:
                if file.exists():
                    for line in file.read_text().splitlines():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
            report = {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "python": sys.version.split()[0],
                "platform": platform.system(),
                "counters": dict(self.counters),
                "timings": self.timings,
                "incidents": events[-100:],
            }
            path = self.directory / "report.json"
            temporary = self.directory / f"report-{self.session}.tmp"
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            temporary.chmod(0o600)
            temporary.replace(path)
            return path

    def close(self):
        self._handler.close()


def observed(name: str):
    """Instrument an async boundary without changing its return value or errors."""

    def decorate(function):
        @wraps(function)
        async def wrapped(self, *args, **kwargs):
            telemetry = getattr(self, "telemetry", None)
            with telemetry.operation(name) if telemetry else nullcontext():
                return await function(self, *args, **kwargs)

        return wrapped

    return decorate
