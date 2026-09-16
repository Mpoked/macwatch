import dataclasses

import pytest

from macwatch.models import Specs, Verdict
from macwatch.pricing import assess, battery_penalty, lookup_reference

AIR_M1_256 = Specs(family="air", chip="m1", ram_gb=8, ssd_gb=256, eligible=True)


def test_reference_exact_key(cfg):
    assert lookup_reference("bazos", AIR_M1_256, cfg.pricing) == (8900, "bazos|air|m1|256", "config")


def test_reference_falls_back_to_chip_without_ssd(cfg):
    specs = dataclasses.replace(AIR_M1_256, ssd_gb=1024)
    assert lookup_reference("bazos", specs, cfg.pricing) == (8900, "bazos|air|m1", "config")


def test_reference_for_other_source_uses_bazos(cfg):
    assert lookup_reference("facebook", AIR_M1_256, cfg.pricing) == \
        (8900, "bazos|air|m1|256", "config:bazos")


def test_own_source_reference_wins(config_data, tmp_path):
    from macwatch.config import parse_config
    config_data["pricing"]["reference"]["facebook|air|m1|256"] = 8000
    cfg = parse_config(config_data, tmp_path)
    assert lookup_reference("facebook", AIR_M1_256, cfg.pricing)[2] == "config"


class TestBatteryPenalty:
    @pytest.mark.parametrize("battery, cycles, penalty", [
        (90, 200, 0),
        (84, 200, 1000),
        (90, 501, 1000),
        (80, 700, 1000),  # obě podmínky, strhne se jen jednou
        (None, None, 0),
    ])
    def test_once(self, cfg, battery, cycles, penalty):
        specs = dataclasses.replace(AIR_M1_256, battery_pct=battery, cycles=cycles)
        assert battery_penalty(specs, cfg.pricing) == penalty


class TestVerdict:
    def test_buy(self, cfg):
        a = assess("bazos", 5500, AIR_M1_256, cfg.pricing)  # limit 0.65 * 8900 = 5785
        assert a.verdict == Verdict.KOUPIT
        assert a.should_alert
        assert a.target_czk == 5785
        assert a.est_profit_czk == 3400
        assert a.roi == pytest.approx(3400 / 5500, abs=1e-4)

    def test_min_profit_buys_expensive_model(self, cfg):
        pricing = dataclasses.replace(cfg.pricing, buy_min_profit_czk=3000)
        a = assess("bazos", 5900, AIR_M1_256, pricing)  # 8900 - 3000, poměrem by to bylo 5785
        assert a.verdict == Verdict.KOUPIT and a.should_alert
        assert a.target_czk == 5900
        assert assess("bazos", 5901, AIR_M1_256, pricing).verdict == Verdict.SLEDOVAT

    def test_min_profit_does_not_tighten_ratio(self, cfg):
        pricing = dataclasses.replace(cfg.pricing, buy_min_profit_czk=5000)
        assert assess("bazos", 5785, AIR_M1_256, pricing).target_czk == 5785

    def test_watch_does_not_alert(self, cfg):
        a = assess("bazos", 7000, AIR_M1_256, cfg.pricing)  # limit 0.80 * 8900 = 7120
        assert a.verdict == Verdict.SLEDOVAT
        assert not a.should_alert

    def test_not_worth_it(self, cfg):
        assert assess("bazos", 9000, AIR_M1_256, cfg.pricing).verdict == Verdict.NEVYPLATI_SE

    def test_red_flag_under_buy_alerts_with_risk(self, cfg):
        specs = dataclasses.replace(AIR_M1_256, red_flags=("icloud",))
        a = assess("bazos", 5000, specs, cfg.pricing)
        assert a.verdict == Verdict.KOUPIT_S_RIZIKEM
        assert a.should_alert

    def test_red_flag_in_watch_band_stays_quiet(self, cfg):
        specs = dataclasses.replace(AIR_M1_256, red_flags=("icloud",))
        a = assess("bazos", 7000, specs, cfg.pricing)
        assert a.verdict == Verdict.SLEDOVAT
        assert not a.should_alert

    def test_battery_penalty_lowers_limits(self, cfg):
        specs = dataclasses.replace(AIR_M1_256, battery_pct=80)
        a = assess("bazos", 5500, specs, cfg.pricing)  # (8900 - 1000) * 0.65 = 5135
        assert a.adjusted_reference_czk == 7900
        assert a.verdict == Verdict.SLEDOVAT

    def test_dealer_not_alerted_by_default(self, cfg):
        specs = dataclasses.replace(AIR_M1_256, is_dealer=True)
        a = assess("bazos", 5000, specs, cfg.pricing)
        assert a.verdict == Verdict.KOUPIT
        assert not a.should_alert

    def test_dealer_alert_can_be_enabled(self, config_data, tmp_path):
        from macwatch.config import parse_config
        config_data["pricing"]["alert_dealers"] = True
        cfg = parse_config(config_data, tmp_path)
        specs = dataclasses.replace(AIR_M1_256, is_dealer=True)
        assert assess("bazos", 5000, specs, cfg.pricing).should_alert

    def test_unknown_model_cheap_alerts(self, cfg):
        specs = Specs(family="pro", chip="m3 max", eligible=True)
        assert assess("bazos", 7900, specs, cfg.pricing).should_alert
        assert not assess("bazos", 30000, specs, cfg.pricing).should_alert

    def test_out_of_scope(self, cfg):
        a = assess("bazos", 1000, Specs(eligible=False, reason="procesor Intel"), cfg.pricing)
        assert a.verdict == Verdict.MIMO and not a.should_alert

    def test_no_price(self, cfg):
        assert assess("bazos", None, AIR_M1_256, cfg.pricing).verdict == Verdict.BEZ_CENY

    def test_score_orders_by_roi(self, cfg):
        cheap = assess("bazos", 4000, AIR_M1_256, cfg.pricing)
        pricier = assess("bazos", 5500, AIR_M1_256, cfg.pricing)
        assert cheap.score > pricier.score


class TestSuspiciousPrice:
    def test_below_ratio_of_reference(self, cfg):
        a = assess("bazos", 2600, AIR_M1_256, cfg.pricing)   # 2 600 < 0,3 × 8 900 = 2 670
        assert a.verdict == Verdict.PODEZRELE
        assert not a.should_alert
        assert a.reference_czk == 8900
        assert "podezřele nízká cena" in a.notes[0]

    def test_just_above_ratio_is_normal_buy(self, cfg):
        a = assess("bazos", 3100, AIR_M1_256, cfg.pricing)
        assert a.verdict == Verdict.KOUPIT and a.should_alert

    def test_absolute_floor_for_unknown_model(self, cfg):
        box = Specs(family="pro", chip="m4 max", eligible=True)
        assert assess("bazos", 600, box, cfg.pricing).verdict == Verdict.PODEZRELE
        assert assess("bazos", 7900, box, cfg.pricing).verdict == Verdict.NEZNAMY_MODEL

    def test_can_be_disabled(self, cfg):
        off = dataclasses.replace(cfg.pricing, suspicious_below_ratio=0, suspicious_below_czk=0)
        assert assess("bazos", 200, AIR_M1_256, off).verdict == Verdict.KOUPIT

    def test_ratio_must_stay_below_buy_ratio(self, config_data, tmp_path):
        from macwatch.config import ConfigError, parse_config
        config_data["pricing"]["suspicious_below_ratio"] = 0.7
        with pytest.raises(ConfigError):
            parse_config(config_data, tmp_path)
