import asyncio
import json
import tempfile
from pathlib import Path

from src.config.config import CodexConfig
from src.core.telemetry import Telemetry, observed
from src.services.prompts import INTERVIEW_PROMPT

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["reply", "wait", "done", "needs_input"]},
        "text": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "text", "reason"],
}


class CodexFailure(RuntimeError):
    def __init__(self, code: str):
        self.diagnostic_code = code
        super().__init__(f"Сбой Codex ({code}). Проверь /diagnostics.")


class CodexTimeout(TimeoutError):
    diagnostic_code = "timeout"


def classify_failure(path: Path) -> str:
    # Raw CLI output is temporary: retain only a fixed diagnostic category.
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65_536))
        tail = stream.read().decode("utf-8", errors="replace").lower()
    for code, markers in (
        ("auth", ("401", "unauthorized", "authentication", "not logged in")),
        ("rate_limit", ("429", "rate_limit", "usage limit", "quota", "credit_balance")),
        ("access", ("403", "permission denied", "access denied")),
        ("network", ("connection refused", "connection reset", "dns", "network error")),
    ):
        if any(marker in tail for marker in markers):
            return code
    return "unknown"


class CodexClient:
    def __init__(self, config: CodexConfig, telemetry: Telemetry | None = None):
        self.telemetry = telemetry
        self.config = config

    @observed("codex")
    async def answer(self, materials, job, history, incoming):
        payload = {
            "candidate": materials,
            "vacancy": job["text"],
            "history": history,
            "incoming": incoming,
        }
        prompt = INTERVIEW_PROMPT + json.dumps(payload, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="recruiter-model-") as tmp:
            folder = Path(tmp)
            schema = folder / "schema.json"
            output = folder / "answer.json"
            schema.write_text(json.dumps(SCHEMA))
            args = [
                self.config.binary,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "-c",
                "features.shell_tool=false",
                "--output-schema",
                str(schema),
                "-o",
                str(output),
            ]
            if self.config.model:
                args += ["-m", self.config.model]
            stderr_path = folder / "stderr.log"
            with stderr_path.open("wb") as stderr_stream:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    "-",
                    cwd=tmp,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=stderr_stream,
                )
                try:
                    await asyncio.wait_for(
                        process.communicate(prompt.encode()),
                        timeout=self.config.timeout,
                    )
                except BaseException as exc:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    if isinstance(exc, TimeoutError):
                        raise CodexTimeout("Превышен таймаут Codex.") from None
                    raise
            if process.returncode or not output.exists():
                raise CodexFailure(classify_failure(stderr_path))
            try:
                return validate_answer(json.loads(output.read_text()))
            except (ValueError, TypeError) as exc:
                raise CodexFailure("invalid_response") from exc


def validate_answer(result):
    if (
        not isinstance(result, dict)
        or set(result) != {"action", "text", "reason"}
        or result["action"] not in SCHEMA["properties"]["action"]["enum"]
        or not all(isinstance(result[k], str) for k in ("text", "reason"))
    ):
        raise ValueError("Некорректный ответ Codex.")
    if result["action"] == "reply" and not 1 <= len(result["text"].strip()) <= 3500:
        raise ValueError("Пустой или слишком длинный ответ.")
    return result
