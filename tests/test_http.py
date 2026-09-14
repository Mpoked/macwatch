import io
import urllib.error
from email.message import Message

import pytest

from macwatch.config import HttpConfig
from macwatch.sources.http import FetchError, PoliteClient

CFG = HttpConfig(user_agent="test-agent", min_delay_seconds=4, timeout_seconds=5, max_retries=2)


class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, url: str, status: int = 200):
        super().__init__(body)
        self.status = status
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def geturl(self):
        return self._url


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(handler):
    clock = Clock()
    client = PoliteClient(CFG, opener=handler, sleep=clock.sleep, clock=clock.time)
    return client, clock


def test_sends_user_agent_and_decodes():
    seen = []

    def handler(request, timeout):
        seen.append(request.get_header("User-agent"))
        return FakeResp("čeština".encode(), request.full_url)

    client, _ = make_client(handler)
    assert client.get("https://pc.bazos.cz/a").text == "čeština"
    assert seen == ["test-agent"]


def test_keeps_delay_between_requests_on_same_site():
    client, clock = make_client(lambda r, t: FakeResp(b"ok", r.full_url))
    client.get("https://www.bazos.cz/rss.php")
    clock.now += 1
    client.get("https://pc.bazos.cz/inzerat/1/x.php")  # jiná subdoména, stejný server
    assert clock.sleeps == [pytest.approx(3.0)]


def test_other_site_does_not_wait():
    client, clock = make_client(lambda r, t: FakeResp(b"ok", r.full_url))
    client.get("https://www.bazos.cz/a")
    client.get("http://localhost:5000/rss")
    assert clock.sleeps == []


def test_gone_is_returned_not_raised():
    def handler(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 410, "Gone", Message(),
                                     io.BytesIO("Inzerát je již vymazán.".encode()))

    client, _ = make_client(handler)
    response = client.get("https://pc.bazos.cz/inzerat/1/x.php")
    assert response.status == 410
    assert "vymazán" in response.text


def test_retries_server_errors_with_backoff():
    calls = []

    def handler(request, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", Message(), None)
        return FakeResp(b"ok", request.full_url)

    client, clock = make_client(handler)
    assert client.get("https://www.bazos.cz/a").text == "ok"
    assert len(calls) == 3
    assert clock.sleeps[0] == 12  # 4 * 3^1


def test_gives_up_after_retries():
    def handler(request, timeout):
        raise urllib.error.URLError("dns")

    client, _ = make_client(handler)
    with pytest.raises(FetchError):
        client.get("https://www.bazos.cz/a")
