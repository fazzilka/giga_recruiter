import logging

import httpx

from src.config.config import AlertConfig

log = logging.getLogger("recruiter.alerts")


class AlertService:
    def __init__(self, http: httpx.AsyncClient, config: AlertConfig):
        self.http, self.config = http, config

    async def send(self, text: str, *, owner_id: int | None = None) -> bool:
        token = self.config.bot_token.get_secret_value()
        chat_id = self.config.chat_id if self.config.chat_id is not None else owner_id
        if not token or not chat_id:
            return False
        payload = {
            "chat_id": chat_id,
            "text": "⚠️ Recruiter · Требуется внимание\n\n"
            + text[:3500]
            + "\n\nКоманды отправляй в Избранном основного аккаунта.",
            "disable_notification": False,
            "link_preview_options": {"is_disabled": True},
        }
        if self.config.topic_id is not None:
            payload["message_thread_id"] = self.config.topic_id
        try:
            response = await self.http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=5,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                log.warning(
                    "Telegram rejected alert delivery; check bot and destination"
                )
                return False
        except (httpx.HTTPError, ValueError) as exc:
            # Bot API URLs contain the token. Never log the exception or response body.
            log.warning(
                "Alert delivery failed: %s status=%s; check terminal and diagnostics",
                type(exc).__name__,
                getattr(getattr(exc, "response", None), "status_code", "-"),
            )
            return False
        log.info("Attention alert delivered")
        return True
