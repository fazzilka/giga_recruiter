import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.config.config import AlertConfig, Config
from src.core.logger import setup_logging
from src.services.alerts import AlertService
from src.userbot.services.commands import ChatCommandService
from src.userbot.services.telegram import TelegramGateway


@pytest.mark.parametrize("group", [False, True])
async def test_attention_goes_to_bot_chat_while_regular_messages_stay_in_saved(group):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    config = Config(
        alerts=AlertConfig(
            bot_token="123:fake-test-token",
            chat_id=-100123 if group else None,
            topic_id=42 if group else None,
        )
    )
    client = SimpleNamespace(send_message=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        alerts = AlertService(http, config.alerts)
        gateway = TelegramGateway(client, config, alerts=alerts)
        gateway.owner_id = 12345
        await gateway.notify("Цикл включён.")
        assert not requests
        await gateway.notify("Нужно уточнение: график? /answer текст", attention=True)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.host == "api.telegram.org"
    assert request.url.path == "/bot123:fake-test-token/sendMessage"
    payload = json.loads(request.content)
    assert payload["chat_id"] == (-100123 if group else 12345)
    assert payload.get("message_thread_id") == (42 if group else None)
    assert payload["disable_notification"] is False
    assert "Нужно уточнение: график? /answer текст" in payload["text"]
    assert "Избранном" in payload["text"]
    assert [c.args[0] for c in client.send_message.call_args_list] == ["me", "me"]


@pytest.mark.parametrize("failure", ["http", "api", "timeout", "malformed"])
async def test_alert_delivery_failure_keeps_saved_message_and_hides_token(
    failure, caplog
):
    token = "123:private-token-do-not-log"

    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(str(request.url), request=request)
        if failure == "http":
            return httpx.Response(401, text=token)
        if failure == "malformed":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": False, "description": token})

    setup_logging()
    config = Config(alerts=AlertConfig(bot_token=token, chat_id=12345))
    client = SimpleNamespace(send_message=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        alerts = AlertService(http, config.alerts)
        gateway = TelegramGateway(client, config, alerts=alerts)
        await gateway.notify("Нужен /recover", attention=True)
    assert client.send_message.call_args.args[:2] == (
        "me",
        "Recruiter · Нужен /recover",
    )
    assert token not in caplog.text
    assert token not in repr(config)


async def test_alert_still_delivered_when_saved_messages_fail():
    delivered = []

    def handle(request):
        delivered.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    config = Config(alerts=AlertConfig(bot_token="123:fake", chat_id=12345))
    client = SimpleNamespace(send_message=AsyncMock(side_effect=OSError("offline")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = TelegramGateway(
            client, config, alerts=AlertService(http, config.alerts)
        )
        with pytest.raises(OSError):
            await gateway.notify("Остановлено. /diagnostics", attention=True)
    assert len(delivered) == 1
    assert "Остановлено. /diagnostics" in delivered[0]["text"]


async def test_disabled_alerts_do_not_call_network_and_test_command_explains_setup():
    def handle(request):
        pytest.fail("Disabled alerts must not use Bot API")

    config = Config()
    client = SimpleNamespace(send_message=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = TelegramGateway(
            client, config, alerts=AlertService(http, config.alerts)
        )
        gateway.owner_id = 12345
        await gateway.notify("Нужно уточнение", attention=True)
        commands = ChatCommandService(
            SimpleNamespace(telegram=gateway), SimpleNamespace()
        )
        result = await commands.execute("/testalert")
    assert "ALERT_BOT_TOKEN" in result


def test_alert_settings_loaded_from_environment_without_exposing_token(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALERT_BOT_TOKEN", "123:fake-secret")
    monkeypatch.setenv("ALERT_CHAT_ID", "-100123")
    monkeypatch.setenv("ALERT_TOPIC_ID", "42")
    config = Config.load(tmp_path)
    assert config.alerts.bot_token.get_secret_value() == "123:fake-secret"
    assert config.alerts.chat_id == -100123
    assert config.alerts.topic_id == 42
    assert "fake-secret" not in repr(config)


async def test_fatal_runtime_error_alerts_group_without_exposing_exception(
    tmp_path, monkeypatch
):
    from src.runtime import app

    delivered = []
    real_client = httpx.AsyncClient

    def handle(request):
        delivered.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    monkeypatch.setattr(app, "_main", AsyncMock(side_effect=OSError("PRIVATE_PAYLOAD")))
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    config = Config(
        root=tmp_path, alerts=AlertConfig(bot_token="123:fake", chat_id=-100123)
    )
    with pytest.raises(OSError, match="PRIVATE_PAYLOAD"):
        await app.main("run", config)
    assert len(delivered) == 1
    assert delivered[0]["chat_id"] == -100123
    assert "/diagnostics" in delivered[0]["text"]
    assert "PRIVATE_PAYLOAD" not in delivered[0]["text"]
