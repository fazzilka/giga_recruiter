import httpx
import pytest

from src.services.vacancies import MAX_REDIRECTS, VacancyService, extract_text


def sber_html(wrapper="styled__VacancyContentWrapper-sc-29vtik-0"):
    return (
        '<html><body><nav>Постороннее меню</nav><div class="' + wrapper + '">'
        "<div><h1>Data Analyst</h1></div><p>Анализ данных и проверка гипотез.</p>"
        "<div>Обязанности</div><p>" + "Проверять гипотезы. " * 12 + "</p>"
        "<div>Требования</div><p>SQL и Python.</p><div>Условия</div><p>Офис.</p>"
        "</div><footer>Посторонний футер</footer></body></html>"
    ).encode()


async def test_sber_redirect_chain_and_vacancy_only():
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/search/4579379":
            return httpx.Response(308, headers={"location": "/search/4579379/"})
        if request.url.path == "/search/4579379/":
            return httpx.Response(
                307, headers={"location": "/search/data-analyst-4579379/"}
            )
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=sber_html()
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as http:
        text = await VacancyService(http, {"rabota.sber.ru"}).text(
            "https://rabota.sber.ru/search/4579379"
        )
    assert requests == [
        "/search/4579379",
        "/search/4579379/",
        "/search/data-analyst-4579379/",
    ]
    assert "SQL и Python" in text
    assert "Постороннее" not in text


def test_sber_structural_fallback_when_css_class_changes():
    text = extract_text(sber_html("new-class"))
    assert text.startswith("Data Analyst")
    assert "Постороннее" not in text


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.test/",
        "http://rabota.sber.ru/",
        "//127.0.0.1/",
        "https://user@rabota.sber.ru/",
    ],
)
async def test_forbidden_redirect_is_rejected_before_second_request(location):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as http:
        with pytest.raises(ValueError, match="запрещённый"):
            await VacancyService(http, {"rabota.sber.ru"}).text(
                "https://rabota.sber.ru/1"
            )
    assert len(requests) == 1


async def test_redirect_loop_is_bounded():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(308, headers={"location": "/same"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ValueError, match="лимит"):
            await VacancyService(http, {"rabota.sber.ru"}).text(
                "https://rabota.sber.ru/same"
            )
    assert len(requests) == MAX_REDIRECTS + 1


async def test_missing_redirect_location():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(307))
    ) as http:
        with pytest.raises(ValueError, match="Location"):
            await VacancyService(http, {"rabota.sber.ru"}).text(
                "https://rabota.sber.ru/1"
            )
