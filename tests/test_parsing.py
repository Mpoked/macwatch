import pytest

from macwatch.parsing import (
    bucket_ssd,
    extract_battery,
    extract_cycles,
    normalize,
    parse_price_czk,
    parse_specs,
    passes_prefilter,
    split_bazos_title,
)


class TestNormalize:
    def test_strips_diacritics_and_case(self):
        assert normalize("Není BLOKOVANÝ, čáry přes displej") == "neni blokovany, cary pres displej"

    def test_removes_emoji_and_unifies_quotes(self):
        assert normalize('MacBook 🔥 Pro 14” ✅') == 'macbook pro 14"'

    def test_keeps_newlines(self):
        assert normalize("řádek 1\n  řádek 2") == "radek 1\n radek 2"


class TestPrice:
    @pytest.mark.parametrize("raw, expected", [
        ("8 900", 8900),
        ("9 000 Kč", 9000),
        ("Kč9 000", 9000),
        ("9 000 Kč", 9000),
        ("9.000,-", 9000),
        ("CZK 9,000", 9000),
        ("12 500,00 Kč", 12500),
        ("500", 500),
        ("Zdarma", 0),
        ("Nabídněte", None),
        ("V textu", None),
        ("Dohodou", None),
        ("", None),
        (None, None),
        ("cca 9 tisíc", None),
    ])
    def test_formats(self, raw, expected):
        assert parse_price_czk(raw) == expected

    def test_title_split_uses_last_colon(self):
        assert split_bazos_title('MacBook Pro 14": M1 Pro: 17 399') == ('MacBook Pro 14": M1 Pro', "17 399")

    def test_title_without_price(self):
        assert split_bazos_title("MacBook bez ceny") == ("MacBook bez ceny", "")


class TestSpecsFromText:
    def test_typical_title(self, rules):
        s = parse_specs("MacBook Air M2 16GB 512GB 2022", "", rules)
        assert (s.family, s.chip, s.ram_gb, s.ssd_gb, s.year, s.eligible) == ("air", "m2", 16, 512, 2022, True)

    def test_slash_pair(self, rules):
        s = parse_specs("Macbook Pro M3 Pro 18/512", "", rules)
        assert (s.family, s.chip, s.ram_gb, s.ssd_gb) == ("pro", "m3 pro", 18, 512)

    def test_cpu_gpu_cores_are_not_ram(self, rules):
        s = parse_specs('MacBook Pro 14" M1 Pro 10/16 - 512GB', "", rules)
        assert s.ram_gb is None
        assert s.ssd_gb == 512

    def test_czech_pro_preposition_on_air(self, rules):
        s = parse_specs("MacBook Air M1 pro studenty", "", rules)
        assert (s.family, s.chip) == ("air", "m1")

    def test_chip_variant_implies_pro(self, rules):
        s = parse_specs("Macbook M2 Max 32GB 1TB", "", rules)
        assert (s.family, s.chip, s.ram_gb, s.ssd_gb) == ("pro", "m2 max", 32, 1024)

    def test_no_diacritics_and_typos(self, rules):
        s = parse_specs("mac book air m1", "baterka 91 % , 212 cyklu, 8 gb ram 256 ssd", rules)
        assert (s.family, s.chip, s.battery_pct, s.cycles, s.ram_gb, s.ssd_gb) == \
            ("air", "m1", 91, 212, 8, 256)

    def test_intel_is_out_of_scope(self, rules):
        s = parse_specs("MacBook Pro 13 2020 i5 16GB", "", rules)
        assert not s.eligible
        assert s.reason == "procesor Intel"

    def test_model_number_gives_family_and_chip(self, rules):
        s = parse_specs("Apple Macbook air 13 silver A2681", "", rules)
        assert (s.family, s.chip, s.eligible) == ("air", "m2", True)
        s = parse_specs("MacBook 14 A2442 16/512", "", rules)
        assert (s.family, s.chip, s.eligible) == ("pro", "m1 pro", True)

    def test_ambiguous_model_number_is_eligible_without_chip(self, rules):
        s = parse_specs("MacBook Pro 13 A2338", "", rules)
        assert (s.family, s.chip, s.eligible) == ("pro", None, True)

    def test_chip_in_title_beats_model_number(self, rules):
        assert parse_specs("MacBook Pro 13 M2 A2338", "", rules).chip == "m2"

    def test_intel_model_number_is_out_of_scope(self, rules):
        s = parse_specs("Macbook pro 2020 a2289", "", rules)
        assert not s.eligible and s.reason == "procesor Intel"
        assert not parse_specs("Prodej macbooku Apple A1466", "", rules).eligible

    def test_year_without_chip_is_eligible(self, rules):
        s = parse_specs("MacBook Air 2022 8/256", "", rules)
        assert s.eligible and s.chip is None

    def test_accessory_is_out_of_scope(self, rules):
        assert not parse_specs("Nabíječka pro MacBook 61W", "", rules).eligible

    def test_formatted_capacity_rounds_to_offered_size(self, rules):
        assert bucket_ssd(245, "gb", rules) == 256
        assert bucket_ssd(1, "tb", rules) == 1024
        assert bucket_ssd(300, "gb", rules) is None


class TestBatteryAndCycles:
    @pytest.mark.parametrize("text, expected", [
        ("stav baterie : 93% (164 cycklu)", 93),
        ("baterie ma 130 cyklu (95% kapacita)", 95),
        ("kapacita baterie 86 %", 86),
        ("baterie ma jen 80 cyklu a je v perfektnim stavu 98%", 98),
        ("sleva 10 % pri rychlem jednani", None),
    ])
    def test_battery(self, text, expected):
        assert extract_battery(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("164 cycklu", 164),
        ("pocet cyklu: 57", 57),
        ("battery cycles 320", 320),
        ("bez cisla", None),
    ])
    def test_cycles(self, text, expected):
        assert extract_cycles(text) == expected


class TestRedFlagsAndNegation:
    @pytest.mark.parametrize("body, flags", [
        ("Displej prasklý v rohu.", ("poskozeni",)),
        ("Není prasklý, bez poškození.", ()),
        ("Žádné praskliny ani škrábance.", ()),
        ("Nepoškozený, plně funkční.", ()),
        ("Zamčený na iCloud, prodávám na díly.", ("icloud", "na_dily")),
        ("Není blokovaný na iCloud, iCloud odhlášený.", ()),
        ("iCloud zámek: ne", ()),
        ("Má MDM od firmy.", ("mdm",)),
        ("Bez MDM, bez iCloud zámku.", ()),
        ("Tři čáry přes displej, jinak ok.", ("poskozeni",)),
        ("Nezapne se.", ("na_dily",)),
        ("Není nefunkční. Ale displej prasklý.", ("poskozeni",)),
    ])
    def test_flags(self, rules, body, flags):
        assert parse_specs("MacBook Air M1", body, rules).red_flags == flags

    @pytest.mark.parametrize("body, dealer", [
        ("Kód: 1623P02\nZáruka 1 měsíc", True),
        ("Záruka 12 měsíců, výdejní místo Praha", True),
        ("Na fakturu s DPH, vystavím fakturu.", True),
        ("Záruka do 2027 u Apple, originální faktura k dispozici.", False),
        ("Prodávám svůj notebook, jen osobní předání.", False),
    ])
    def test_dealer(self, rules, body, dealer):
        assert parse_specs("MacBook Air M1", body, rules).is_dealer is dealer


def test_prefilter(rules):
    assert passes_prefilter("Apple MacBook Air M1", rules)
    assert passes_prefilter("mac book pro", rules)
    assert not passes_prefilter("Lenovo ThinkPad T14", rules)
