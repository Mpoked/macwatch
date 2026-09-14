import dataclasses

import pytest

from macwatch.config import TravelSettings
from macwatch.geo import default_gazetteer, estimate_travel, haversine_km
from macwatch.models import Specs, Verdict
from macwatch.pricing import assess

AIR_M1_256 = Specs(family="air", chip="m1", ram_gb=8, ssd_gb=256, eligible=True)


@pytest.fixture(scope="module")
def gaz():
    return default_gazetteer()


def test_exact_postal_code(gaz):
    place = gaz.by_postal_code("686 01")
    assert place.exact
    assert 49.0 < place.lat < 49.15 and 17.2 < place.lon < 17.55


def test_missing_code_uses_nearest(gaz):
    place = gaz.by_postal_code("65605")  # zvláštní kód pro velké příjemce, v datech není
    assert place is not None and not place.exact
    brno = gaz.by_place_name("Brno")
    assert haversine_km(place, brno) < 40


def test_invalid_code(gaz):
    assert gaz.by_postal_code("123") is None
    assert gaz.by_postal_code(None) is None


@pytest.mark.parametrize("name", ["Ostrava", "Nesovice", "Zlín", "zlin", "Praha 7",
                                  "Brno, Czech Republic", "Uherské Hradiště"])
def test_place_names(gaz, name):
    assert gaz.by_place_name(name) is not None, name


def test_unknown_place(gaz):
    assert gaz.by_place_name("Atlantida") is None


def test_distance_prague_brno(gaz):
    km = haversine_km(gaz.by_place_name("Praha 7"), gaz.by_place_name("Brno"))
    assert 170 < km < 200


def test_travel_cost_from_consumption(gaz):
    settings = TravelSettings(home_psc="68601", consumption_l_per_100km=6.0,
                              fuel_price_czk_per_l=40.0, road_factor=1.0, round_trip=True)
    home = gaz.by_postal_code("68601")
    target = gaz.by_place_name("Zlín")
    travel = estimate_travel(settings, home, target)
    expected_km = haversine_km(home, target)
    assert travel.distance_km == pytest.approx(expected_km, abs=0.1)
    assert travel.cost_czk == round(expected_km * 2 * 2.4)  # 6 l/100 km * 40 Kč = 2,40 Kč/km
    assert travel.within_limit


def test_max_km_and_unlimited(gaz):
    home, far = gaz.by_postal_code("68601"), gaz.by_place_name("Ostrava")
    limited = estimate_travel(TravelSettings(max_km=50), home, far)
    assert not limited.within_limit
    assert estimate_travel(TravelSettings(max_km=None), home, far).within_limit


def test_travel_disabled(gaz):
    assert estimate_travel(TravelSettings(enabled=False), gaz.by_place_name("Zlín"),
                           gaz.by_place_name("Brno")) is None


class TestTravelInAssessment:
    def travel(self, gaz, cost, within=True):
        t = estimate_travel(TravelSettings(), gaz.by_place_name("Zlín"), gaz.by_place_name("Brno"))
        return dataclasses.replace(t, cost_czk=cost, within_limit=within)

    def test_travel_cost_lowers_profit_and_can_flip_verdict(self, cfg, gaz):
        near = assess("bazos", 5500, AIR_M1_256, cfg.pricing, travel=self.travel(gaz, 0))
        far = assess("bazos", 5500, AIR_M1_256, cfg.pricing, travel=self.travel(gaz, 600))
        assert near.verdict == Verdict.KOUPIT  # limit 5785
        assert far.verdict == Verdict.SLEDOVAT  # 5500 + 600 > 5785
        assert far.est_profit_czk == near.est_profit_czk - 600
        assert far.travel_cost_czk == 600 and far.distance_km is not None

    def test_beyond_max_km_is_not_alerted(self, cfg, gaz):
        a = assess("bazos", 3000, AIR_M1_256, cfg.pricing, travel=self.travel(gaz, 100, within=False))
        assert a.verdict == Verdict.KOUPIT
        assert not a.should_alert
        assert any("mimo dojezd" in n for n in a.notes)
