import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.config import Config
from src.config.config import CodexConfig
from src.core.telemetry import Telemetry
from src.integrations.codex import CodexClient, CodexFailure
from src.runtime.app import main
from src.runtime.interview import InterviewScheduler
from src.userbot.handlers.commands import UserbotCommandHandlers
from src.userbot.services.commands import ChatCommandService


@pytest.fixture
def telemetry(tmp_path):
    service = Telemetry(tmp_path / "diagnostics")
    try:
        yield service
    finally:
        service.close()


def test_metrics_persist_across_restart(telemetry):
    with telemetry.operation("codex"):
        pass
    with pytest.raises(TimeoutError):
        with telemetry.operation("codex"):
            raise TimeoutError("private payload")
    restored = Telemetry(telemetry.directory)
    try:
        assert restored.counters["codex.ok"] == 1
        assert restored.counters["codex.failed"] == 1
        assert restored.timings["codex"]["count"] == 2
        assert restored.timings["codex"]["max_seconds"] >= 0
    finally:
        restored.close()


def test_error_report_has_stack_but_no_messages_locals_or_secret_paths(telemetry):
    secret = "PRIVATE_RESUME_API_HASH_PHONE_NUMBER"
    try:
        try:
            raise ValueError(secret)
        except ValueError as cause:
            raise RuntimeError(secret) from cause
    except RuntimeError as exc:
        incident = telemetry.error("interview.failed", exc, phase="sending")
    path = telemetry.directory / "report.json"
    raw = path.read_text()
    report = json.loads(raw)
    assert secret not in raw
    assert str(Path.home()) not in raw
    assert report["incidents"][0]["incident"] == incident
    assert report["incidents"][0]["exception"] == "RuntimeError"
    assert report["incidents"][0]["causes"] == ["ValueError"]
    assert report["incidents"][0]["stack"]
    assert set(report["incidents"][0]["stack"][0]) == {"file", "line", "function"}
    assert path.stat().st_mode & 0o777 == 0o600


async def test_scheduler_records_failure_and_notifies_incident_id(telemetry):
    service = SimpleNamespace(
        busy=asyncio.Lock(),
        current={"phase": "sending"},
        tick=AsyncMock(side_effect=OSError("private text")),
        pause=Mock(),
        telegram=SimpleNamespace(notify=AsyncMock()),
    )
    await InterviewScheduler(service, telemetry).step()
    assert telemetry.counters["interview.step.failed"] == 1
    assert telemetry.counters["errors.interview.failed"] == 1
    service.pause.assert_called_once()
    assert "/diagnostics" in service.telegram.notify.call_args.args[0]
    assert "private text" not in (telemetry.directory / "report.json").read_text()


async def test_commands_produce_metrics_and_report(telemetry):
    commands = ChatCommandService(Mock(), Mock(), telemetry)
    assert "Зафиксировано ошибок" in await commands.execute("/metrics")
    assert "report.json" in await commands.execute("/diagnostics")
    assert telemetry.counters["command.ok"] == 2


async def test_command_failure_does_not_log_command_arguments(telemetry):
    commands = SimpleNamespace(
        telemetry=telemetry,
        interview=SimpleNamespace(current=None, pause=Mock()),
        execute=AsyncMock(side_effect=OSError("SECRET")),
    )
    telegram = SimpleNamespace(notify=AsyncMock())
    await UserbotCommandHandlers(commands, telegram).handle(
        SimpleNamespace(raw_text="/answer SECRET")
    )
    assert telemetry.counters["errors.command.failed"] == 1
    assert "SECRET" not in telemetry.export().read_text()


async def test_cancelled_operation_is_not_failed(telemetry):
    with pytest.raises(asyncio.CancelledError):
        with telemetry.operation("codex"):
            raise asyncio.CancelledError()
    assert telemetry.counters["codex.cancelled"] == 1
    assert not telemetry.counters["codex.failed"]


def test_rotation_bounds_event_files_and_export(telemetry):
    telemetry._handler.maxBytes = 600
    for _ in range(15):
        telemetry.error("runtime.background", RuntimeError("SECRET"))
    assert len(list(telemetry.directory.glob("events.jsonl*"))) <= 4
    assert "SECRET" not in telemetry.export().read_text()
    assert len(json.loads(telemetry.export().read_text())["incidents"]) <= 100


def test_corrupt_metrics_file_does_not_prevent_startup(tmp_path):
    (tmp_path / "metrics.json").write_text("invalid JSON")
    telemetry = Telemetry(tmp_path)
    try:
        assert telemetry.counters["diagnostics.load_failed"] == 1
    finally:
        telemetry.close()


def test_disk_write_failure_does_not_replace_original_exception(telemetry, monkeypatch):
    def fail(*args):
        raise OSError("Disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(RuntimeError, match="original"):
        with telemetry.operation("codex"):
            raise RuntimeError("original")
    assert telemetry.storage_failed


async def test_offline_diagnostics_without_telegram_credentials(tmp_path, capsys):
    await main("diagnostics", Config(root=tmp_path))
    report = tmp_path / "state" / "diagnostics" / "report.json"
    assert report.exists()
    assert str(report) in capsys.readouterr().out


async def test_startup_failure_is_saved_before_exit(tmp_path):
    with pytest.raises(SystemExit):
        await main("run", Config(root=tmp_path))
    report = json.loads(
        (tmp_path / "state" / "diagnostics" / "report.json").read_text()
    )
    assert report["counters"]["runtime.failed"] == 1
    assert report["incidents"][0]["exception"] == "SystemExit"


async def test_codex_failure_classified_without_exporting_stderr(tmp_path, telemetry):
    fake = tmp_path / "fake-codex"
    fake.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        'sys.stderr.write("401 Unauthorized: PRIVATE_API_KEY_AND_RESUME")\nsys.exit(1)\n'
    )
    fake.chmod(0o700)
    with pytest.raises(CodexFailure) as failure:
        await CodexClient(CodexConfig(binary=str(fake)), telemetry).answer(
            "PRIVATE_RESUME", {"text": "SQL"}, [], ["Вопрос"]
        )
    assert failure.value.diagnostic_code == "auth"
    telemetry.error("interview.failed", failure.value)
    report = telemetry.export().read_text()
    assert "PRIVATE" not in report
    assert '"code": "auth"' in report
    assert telemetry.counters["codex.failed"] == 1


@pytest.mark.parametrize(
    "saved",
    [
        {"counters": [], "timings": {}},
        {"counters": {"codex.ok": "wrong"}, "timings": {}},
        {"counters": {}, "timings": {"codex": {"count": 0}}},
    ],
)
def test_invalid_metrics_shape_does_not_break_diagnostics(tmp_path, saved):
    (tmp_path / "metrics.json").write_text(json.dumps(saved))
    telemetry = Telemetry(tmp_path)
    try:
        assert telemetry.counters["diagnostics.load_failed"] == 1
        assert telemetry.summary()
    finally:
        telemetry.close()


def test_http_status_saved_in_report_without_url_or_body(telemetry):
    import httpx

    request = httpx.Request("GET", "https://example.test/PRIVATE?token=SECRET")
    response = httpx.Response(403, request=request, text="PRIVATE RESPONSE")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        telemetry.error("command.failed", exc)
    report = telemetry.export().read_text()
    assert '"http_status": 403' in report
    assert "SECRET" not in report and "PRIVATE" not in report


def test_console_logs_operations_and_sanitized_stack(telemetry, capsys):
    from src.core.logger import setup_logging

    setup_logging()
    with telemetry.operation("codex"):
        pass
    try:
        raise RuntimeError("PRIVATE_SECRET")
    except RuntimeError as exc:
        telemetry.error("command.failed", exc)
    output = capsys.readouterr().err
    assert "codex started" in output and "duration=" in output
    assert "incident=" in output and "test_telemetry.py:" in output
    assert "PRIVATE_SECRET" not in output
