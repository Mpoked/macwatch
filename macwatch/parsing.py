"""Extrakce konfigurace z textu inzerátu. Společná pro všechny zdroje.

Pravidla specifická pro druh zboží (rodiny, čipy, červené vlajky, obchodníci)
jsou v configu. Tady jsou jen obecné extraktory: cena, kapacity, procenta,
počty a vyhodnocení negací.
"""

from __future__ import annotations

import re
import unicodedata

from macwatch.config import ParsingRules
from macwatch.models import Specs

# normalizace

_QUOTES = str.maketrans({"”": '"', "“": '"', "„": '"', "″": '"', "’": "'", "‘": "'",
                         " ": " ", " ": " ", "–": "-", "—": "-"})


def normalize(text: str) -> str:
    """Malá písmena, bez diakritiky, bez emoji, zachová konce řádků."""
    text = unicodedata.normalize("NFKD", text.translate(_QUOTES))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^\x00-\x7f]", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()


# cena

_PRICE_WORDS_NONE = ("nabidnete", "v textu", "dohodou", "dohoda", "info v textu", "cena v textu")
_PRICE_WORDS_FREE = ("zdarma", "free", "daruji")


def split_bazos_title(title: str) -> tuple[str, str]:
    """Titulek z RSS Bazoše má cenu za poslední dvojtečkou: ``Název: 8 900``."""
    head, sep, tail = title.rpartition(":")
    if not sep:
        return title.strip(), ""
    return head.strip(), tail.strip()


def parse_price_czk(raw: str | None) -> int | None:
    """Převede cenu v libovolném běžném tvaru na celé koruny.

    ``8 900``, ``9 000 Kč``, ``Kč9 000``, ``9.000,-``, ``CZK 9,000`` dají 9000.
    ``Zdarma`` dá 0. ``Nabídněte``, ``V textu``, ``Dohodou`` a nesmysly dají None.
    """
    if not raw:
        return None
    text = normalize(raw)
    if any(word in text for word in _PRICE_WORDS_FREE):
        return 0
    if any(word in text for word in _PRICE_WORDS_NONE):
        return None
    # měna bez hranice slova, Facebook píše i "Kč9 000"
    text = re.sub(r"czk|kc|korun[a-z]*|,-|\.-", " ", text)
    text = re.sub(r"(?<=\d)[,.]\d{2}\s*$", "", text.strip())  # haléře
    digits = re.fullmatch(r"\s*(\d{1,3}(?:[ .,]\d{3})+|\d+)\s*", text)
    if not digits:
        return None
    return int(re.sub(r"[ .,]", "", digits.group(1)))


# obecné pomocníky

def _first(patterns: tuple[re.Pattern[str], ...], text: str) -> re.Match[str] | None:
    best = None
    for pattern in patterns:
        match = pattern.search(text)
        if match and (best is None or match.start() < best.start()):
            best = match
    return best


_WORD = re.compile(r"[a-z]+")
_SENTENCE_BREAK = re.compile(r"[.!?;\n]|\bale\b|\bjen\b")


def is_negated(text: str, start: int, end: int, rules: ParsingRules) -> bool:
    """Je nález v textu popřený? Hledá zápor těsně před ním a těsně za ním.

    ``neni blokovany``, ``bez poskozeni``, ``zadne praskliny`` a
    ``icloud zamek: ne`` jsou popřené. Hranice věty zápor ukončí.
    """
    before = text[max(0, start - 40):start]
    breaks = list(_SENTENCE_BREAK.finditer(before))
    if breaks:
        before = before[breaks[-1].end():]
    if any(word in rules.negations_before for word in _WORD.findall(before)[-3:]):
        return True
    after = text[end:end + 25]
    cut = _SENTENCE_BREAK.search(after)
    if cut:
        after = after[:cut.start()]
    return any(word in rules.negations_after for word in _WORD.findall(after)[:2])


def _find_affirmed(patterns: tuple[re.Pattern[str], ...], text: str,
                   rules: ParsingRules) -> bool:
    for pattern in patterns:
        for match in pattern.finditer(text):
            if not is_negated(text, match.start(), match.end(), rules):
                return True
    return False


# jednotlivé extraktory, vstupem je normalizovaný text

def extract_family(text: str, chip_variant: str | None, rules: ParsingRules) -> str | None:
    hits = [(m.start(), name) for name, pats in rules.families.items()
            if (m := _first(pats, text))]
    if hits:
        return min(hits)[1]
    if chip_variant in ("pro", "max"):
        return "pro"
    hits = [(m.start(), name) for name, pats in rules.family_words.items()
            if (m := _first(pats, text))]
    return min(hits)[1] if hits else None


def extract_chip(text: str, rules: ParsingRules) -> tuple[str | None, int | None, str | None]:
    """Vrátí (čip, generace, varianta), např. ("m1 pro", 1, "pro")."""
    match = rules.chip_pattern.search(text)
    if not match:
        return None, None, None
    generation = int(match.group(1))
    variant = match.group(2) if match.lastindex and match.lastindex >= 2 else None
    chip = f"m{generation}" + (f" {variant}" if variant else "")
    return chip, generation, variant


_RAM_PATTERNS = (
    re.compile(r"\b(\d{1,3})\s*gb\s*(?:ram|unified|jednotn\w*|operacn\w*|pamet\w*)"),
    re.compile(r"\b(?:ram|operacni\s+pamet\w*|pamet\w*)\s*[:=\-]?\s*(\d{1,3})\s*(?:gb)?\b"),
    re.compile(r"\b(\d{1,3})\s*ram\b"),
)
_PAIR = re.compile(r"\b(\d{1,3})\s*(?:gb)?\s*/\s*(\d{1,4})\s*(gb|tb)?\b")
_RAM_LOOSE = re.compile(r"\b(\d{1,3})\s*gb\b")

_SSD_PATTERNS = (
    re.compile(r"\b(\d{1,4})\s*(gb|tb)\s*(?:ssd|disk\w*|uloziste|storage|nvme)"),
    re.compile(r"\b(?:ssd|disk|uloziste|storage)\s*[:=\-]?\s*(\d{1,4})\s*(gb|tb)\b"),
    re.compile(r"\b(\d{3,4})\s*()(?:ssd)\b"),
    re.compile(r"\b(\d)\s*(tb)\b"),
    re.compile(r"\b(\d{3,4})\s*(gb)\b"),
)


def _ram_ok(value: int, rules: ParsingRules) -> bool:
    return value in rules.ram_values_gb


def bucket_ssd(value: int, unit: str | None, rules: ParsingRules) -> int | None:
    """Zaokrouhlí kapacitu na nabízenou velikost. 245 GB je 256, 1 TB je 1024."""
    gb = value * 1024 if unit == "tb" else value
    for size in rules.ssd_values_gb:
        if size * 0.85 <= gb <= size * 1.0 or (unit == "tb" and gb == size):
            return size
    return None


def _pair(text: str, rules: ParsingRules) -> tuple[int | None, int | None]:
    for match in _PAIR.finditer(text):
        ram = int(match.group(1))
        ssd = bucket_ssd(int(match.group(2)), match.group(3), rules)
        if _ram_ok(ram, rules) and ssd and ssd >= 128:
            return ram, ssd
    return None, None


def extract_ram(text: str, rules: ParsingRules) -> int | None:
    for pattern in _RAM_PATTERNS:
        for match in pattern.finditer(text):
            if _ram_ok(int(match.group(1)), rules):
                return int(match.group(1))
    ram, _ = _pair(text, rules)
    if ram:
        return ram
    for match in _RAM_LOOSE.finditer(text):
        value = int(match.group(1))
        # bez slova RAM je 128 GB skoro jistě disk, ne paměť
        if _ram_ok(value, rules) and value < 128:
            return value
    return None


def extract_ssd(text: str, rules: ParsingRules) -> int | None:
    _, ssd = _pair(text, rules)
    if ssd:
        return ssd
    for pattern in _SSD_PATTERNS:
        for match in pattern.finditer(text):
            value = int(match.group(1))
            unit = match.group(2) or "gb"
            if unit == "gb" and value < 100:
                continue
            size = bucket_ssd(value, unit, rules)
            if size:
                return size
    return None


_YEAR = re.compile(r"\b(20(?:1[0-9]|2[0-9]))\b")


def extract_year(text: str) -> int | None:
    match = _YEAR.search(text)
    return int(match.group(1)) if match else None


_BATTERY = (
    re.compile(r"(\d{2,3})\s*%\s*(?:kapacit\w*|zdravi|baterie|baterk\w*|battery|health|stav\w*)"),
    re.compile(r"(?:baterie|baterk\w*|akumulator\w*|battery|kapacit\w*|zdravi|health|"
               r"condition)[^\d\n%]{0,25}?(\d{2,3})\s*%"),
    # "baterie ma jen 80 cyklu a je v perfektnim stavu 98%"
    re.compile(r"\b(?:baterie|baterk\w*|battery)\b[^\n%]{0,60}?\b(\d{2,3})\s*%"),
)


def extract_battery(text: str) -> int | None:
    found = []
    for pattern in _BATTERY:
        for match in pattern.finditer(text):
            value = int(match.group(1))
            if 40 <= value <= 100:
                found.append((match.start(), value))
    return min(found)[1] if found else None


_CYCLES = (
    re.compile(r"\b(\d{1,4})\s*(?:nabijecich\s+)?(?:cy?c?kl\w*|cycl\w*)"),
    re.compile(r"\b(?:cy?c?kl\w*|cycl\w*)\s*(?:baterie\s*)?[:=\-]?\s*(\d{1,4})\b"),
)


def extract_cycles(text: str) -> int | None:
    found = []
    for pattern in _CYCLES:
        for match in pattern.finditer(text):
            found.append((match.start(), int(match.group(1))))
    return min(found)[1] if found else None


def extract_red_flags(text: str, rules: ParsingRules) -> tuple[str, ...]:
    return tuple(name for name, patterns in rules.red_flags.items()
                 if _find_affirmed(patterns, text, rules))


def is_dealer(text: str, rules: ParsingRules) -> bool:
    return _find_affirmed(rules.dealer, text, rules)


def passes_prefilter(title: str, rules: ParsingRules) -> bool:
    text = normalize(title)
    return any(p.search(text) for p in rules.prefilter)


# modelová čísla Apple (A2337 na spodku notebooku), inzeráty z Facebooku mají často jen titulek

# rodina, čip; u Pro/Max varianty se bere slabší čip, ať reference nevychází přehnaně
MODEL_NUMBERS: dict[str, tuple[str, str | None]] = {
    "a2337": ("air", "m1"),
    "a2338": ("pro", None),          # Pro 13" s M1 i M2, stejné číslo
    "a2442": ("pro", "m1 pro"),
    "a2485": ("pro", "m1 pro"),
    "a2681": ("air", "m2"),
    "a2941": ("air", "m2"),
    "a2779": ("pro", "m2 pro"),
    "a2780": ("pro", "m2 pro"),
    "a2918": ("pro", "m3"),
    "a2992": ("pro", "m3 pro"),
    "a2991": ("pro", "m3 pro"),
    "a3113": ("air", "m3"),
    "a3114": ("air", "m3"),
    "a3240": ("air", "m4"),
    "a3241": ("air", "m4"),
    "a3112": ("pro", "m4"),
}
INTEL_MODEL_NUMBERS = frozenset({
    "a1278", "a1286", "a1369", "a1370", "a1398", "a1425", "a1465", "a1466", "a1502", "a1534",
    "a1706", "a1707", "a1708", "a1932", "a1989", "a1990", "a2141", "a2159", "a2179", "a2251", "a2289",
})
_MODEL_NUMBER = re.compile(r"\b(a\d{4})\b")


def extract_model_number(text: str) -> tuple[str, tuple[str, str | None] | None] | None:
    """Vrátí (číslo, (rodina, čip)) pro Apple Silicon, (číslo, None) pro Intel, jinak None."""
    for match in _MODEL_NUMBER.finditer(text):
        number = match.group(1)
        if number in MODEL_NUMBERS:
            return number, MODEL_NUMBERS[number]
        if number in INTEL_MODEL_NUMBERS:
            return number, None
    return None


# celek

def _title_then_body(fn, title: str, body: str, *args):
    value = fn(title, *args)
    return value if value is not None else fn(body, *args)


TITLE_RULED_OUT = frozenset({"procesor Intel", "starý čip", "není to hlídaný produkt"})


def ruled_out_by_title(specs: Specs) -> bool:
    """Má smysl stahovat detail? Ne, když už titulek model jasně vyřadil.

    "čip neuveden" a "neznámá rodina" detail potřebují: v textu bývá M1 i u titulku "MacBook Pro 23".
    """
    if specs.eligible:
        return False
    if specs.reason in TITLE_RULED_OUT:
        return True
    return specs.chip is None and specs.year is not None and specs.year < 2020


def parse_specs(title: str, body: str | None, rules: ParsingRules) -> Specs:
    t = normalize(title)
    b = normalize(body or "")
    both = f"{t}\n{b}"

    chip, generation, variant = extract_chip(t, rules)
    if chip is None:
        chip, generation, variant = extract_chip(b, rules)
    family = extract_family(t, variant, rules) or extract_family(b, variant, rules)
    if family == "air" and variant:
        # "MacBook Air M1 pro studenty": pro je tady předložka, Air nemá čip Pro
        chip, variant = f"m{generation}", None
    year = _title_then_body(extract_year, t, b)
    non_apple = _first(rules.non_apple_cpu, t) or (chip is None and _first(rules.non_apple_cpu, b))
    model = extract_model_number(t) or (extract_model_number(b) if chip is None else None)
    by_number = False
    if chip is None and model is not None:
        if model[1] is None:
            non_apple = True
        else:
            family = family or model[1][0]
            chip, by_number = model[1][1], True
            if chip is not None:
                generation, variant = int(chip[1]), (chip.split()[1] if " " in chip else None)

    if not any(p.search(both) for p in rules.prefilter):
        eligible, reason = False, "není to hlídaný produkt"
    elif family is None:
        eligible, reason = False, "neznámá rodina"
    elif chip is not None and generation is not None:
        if generation < rules.min_chip_generation:
            eligible, reason = False, "starý čip"
        else:
            eligible, reason = True, (f"čip podle modelového čísla {model[0].upper()}" if by_number else None)
    elif non_apple:
        eligible, reason = False, "procesor Intel"
    elif by_number:
        eligible, reason = True, "Apple Silicon podle modelového čísla, M1 nebo M2"
    elif year is not None and year >= rules.min_year_without_chip:
        eligible, reason = True, "čip neuveden, odvozeno z roku"
    else:
        eligible, reason = False, "čip neuveden"

    return Specs(
        family=family,
        chip=chip,
        ram_gb=_title_then_body(extract_ram, t, b, rules),
        ssd_gb=_title_then_body(extract_ssd, t, b, rules),
        year=year,
        battery_pct=extract_battery(both),
        cycles=extract_cycles(both),
        is_dealer=is_dealer(both, rules),
        red_flags=extract_red_flags(both, rules),
        eligible=eligible,
        reason=reason,
    )
