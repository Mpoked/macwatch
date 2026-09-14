import io
import json
import urllib.error
from email.message import Message

from macwatch.models import Assessment, Specs, Verdict
from macwatch.notify import DiscordNotifier, build_embed
from macwatch.privacy import redact_text

SPECS = Specs(family="air", chip="m2", ram_gb=8, ssd_gb=256, eligible=True)


class TestEmbed:
    def test_contains_link_model_price_and_profit(self):
        a = Assessment(Verdict.KOUPIT, True, reference_czk=16000, adjusted_reference_czk=16000,
                       est_profit_czk=4500, roi=0.3913, reference_origin="config")
        embed = build_embed(source="bazos", url="https://x/1", price_czk=11500, specs=SPECS,
                            assessment=a)
        assert embed["title"] == "✅ VYPLATÍ SE: MacBook Air M2 8/256"
        assert embed["url"] == "https://x/1"
        assert "11 500 Kč" in embed["description"]
        assert "+4 500 Kč" in embed["description"]
        assert "ROI 39 %" in embed["description"]
        assert embed["footer"]["text"] == "Bazoš"

    def test_risk_and_borrowed_reference_are_visible(self):
        specs = Specs(family="pro", chip="m1 pro", ram_gb=16, ssd_gb=1024, eligible=True,
                      red_flags=("icloud", "na_dily"))
        a = Assessment(Verdict.KOUPIT_S_RIZIKEM, True, adjusted_reference_czk=20000,
                       est_profit_czk=8000, roi=0.66, reference_origin="config:bazos")
        embed = build_embed(source="facebook", url="u", price_czk=12000, specs=specs,
                            assessment=a)
        assert embed["title"].startswith("⚠️")
        assert "MacBook Pro M1 Pro 16/1TB" in embed["title"]
        assert "Riziko: icloud, na dily" in embed["description"]
        assert "Reference převzatá z Bazoše" in embed["description"]

    def test_unknown_model_has_no_profit(self):
        embed = build_embed(source="bazos", url="u", price_czk=7000,
                            specs=Specs(family="pro", chip="m3", eligible=True),
                            assessment=Assessment(Verdict.NEZNAMY_MODEL, True))
        assert "Odhad zisku" not in embed["description"]
        assert "MacBook Pro M3 ?/?" in embed["title"]


class FakeResponse(io.BytesIO):
    status = 204


class TestNotifier:
    def test_sends_json(self):
        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return FakeResponse(b"")

        assert DiscordNotifier("https://discord/webhook", opener=opener).send({"title": "x"})
        assert sent == [{"embeds": [{"title": "x"}]}]

    def test_retries_after_rate_limit(self):
        calls, sleeps = [], []
        headers = Message()
        headers["Retry-After"] = "1.5"

        def opener(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 429, "Too Many", headers, None)
            return FakeResponse(b"")

        assert DiscordNotifier("https://discord/webhook", opener=opener, sleep=sleeps.append).send({})
        assert sleeps == [1.5]

    def test_gives_up_on_client_error(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError("u", 400, "Bad", Message(), None)

        assert not DiscordNotifier("https://discord/webhook", opener=opener, sleep=lambda s: None).send({})


class TestRedaction:
    def test_phone_formats(self):
        for phone in ("+420777123456", "+420 777 123 456", "777 123 456", "777123456",
                      "00420 777-123-456"):
            assert redact_text(f"volejte {phone} díky") == "volejte [telefon] díky", phone

    def test_email(self):
        assert redact_text("pište na jan.novak+mac@seznam.cz") == "pište na [e-mail]"

    def test_prices_and_configs_stay(self):
        text = "Cena 17 399 Kč, 16/512, kód 1623P02, 164 cyklů, rok 2021"
        assert redact_text(text) == text
