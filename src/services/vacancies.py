import asyncio
import logging
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from src.core.telemetry import Telemetry, observed

log = logging.getLogger("recruiter.vacancies")
REDIRECTS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5


def allowed_url(url, hosts):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in hosts
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
    )


class VacancyService:
    def __init__(
        self,
        client: httpx.AsyncClient,
        hosts: set[str],
        telemetry: Telemetry | None = None,
    ):
        self.telemetry = telemetry
        self.client, self.hosts = client, hosts
        self._cache: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._downloads = asyncio.Semaphore(3)

    @observed("vacancy.fetch")
    async def text(self, url: str) -> str:
        if not allowed_url(url, self.hosts):
            raise ValueError(
                "Ссылка не входит в VACANCY_HOSTS или использует не HTTPS."
            )
        async with self._locks.setdefault(url, asyncio.Lock()):
            if url in self._cache:
                return self._cache[url]
            async with self._downloads:
                body = await self._download(url)
            text = await asyncio.to_thread(extract_text, bytes(body))
            log.info("Vacancy text extracted: %d characters", len(text))
            self._cache[url] = text
            return text

    async def _download(self, url: str) -> bytes:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            # Revalidate every destination before a request; never enable global redirects.
            if not allowed_url(current, self.hosts):
                raise ValueError("Перенаправление ведёт на запрещённый адрес.")
            async with self.client.stream(
                "GET", current, follow_redirects=False
            ) as response:
                log.info(
                    "Vacancy HTTP status=%d redirect_step=%d", response.status_code, hop
                )
                if response.status_code in REDIRECTS:
                    if hop == MAX_REDIRECTS:
                        raise ValueError("Превышен лимит перенаправлений вакансии.")
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError(
                            "Перенаправление вакансии не содержит Location."
                        )
                    current = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", ""):
                    raise ValueError("Вакансия должна быть HTML-страницей.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise ValueError("Страница слишком большая.")
                return bytes(body)
        raise ValueError("Не удалось загрузить вакансию.")


def extract_text(body: bytes) -> str:
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    content = soup.find("main") or soup.find("article")
    heading = soup.find("h1")
    if heading:
        # Sber uses styled divs instead of main/article. Keep only the vacancy wrapper.
        for parent in heading.parents:
            classes = parent.get("class", [])
            if any("styled__VacancyContentWrapper-" in name for name in classes):
                content = parent
                break
        else:
            # Structural fallback if generated CSS class names change.
            for parent in heading.parents:
                if parent.name in {"body", "html", "[document]"}:
                    break
                labels = set(parent.stripped_strings)
                if {"Обязанности", "Требования", "Условия"} <= labels:
                    content = parent
                    break
    if content is None:
        raise ValueError("Не найден основной текст страницы. Используй /addtext.")
    text = content.get_text("\n", strip=True)
    if not 150 <= len(text) <= 30_000:
        raise ValueError("Не удалось извлечь текст вакансии. Используй /addtext.")
    return text
