# macwatch

Hlídá bazary s použitými MacBooky (Air a Pro s čipem M1 a novějším), posoudí cenu
proti referenční tabulce a výhodné kusy pošle na Discord.

Zdroje: Bazoš (RSS a detail) a Facebook Marketplace (přes upravený fb-mp-rss v Dockeru).

## Jak to funguje

1. `sources/bazos.py` stáhne RSS hledání (`rss.php?rub=pc&hledat=macbook`).
   Jeden feed pokryje zhruba 7 hodin inzerátů, interval 4 minuty má velkou rezervu.
2. Titulky projdou předfiltrem, u nových MacBooků se stáhne detail s plným textem
   a lokalitou. Po jednom, s rozestupem 4 s, nejvýš 25 detailů za průchod.
3. `privacy.py` nahradí telefony a e‑maily zástupkou. Jméno prodávajícího se nečte.
4. `parsing.py` vytáhne rodinu, čip, RAM, SSD, rok, baterii, cykly, příznaky obchodníka
   a červené vlajky s ohledem na zápor („není blokovaný“ není problém).
5. `pricing.py` najde referenci podle `zdroj|rodina|čip|ssd`, pak `zdroj|rodina|čip`,
   pak totéž pro Bazoš. Srazí za baterii (jednou), určí verdikt a ROI.
6. Upozornění dostanou jen KOUPIT, KOUPIT_S_RIZIKEM a levný model mimo tabulku,
   a to až po stažení plného textu. Obchodníci se neposílají, dá se zapnout.
7. Vše jde do SQLite: `listings`, `price_history` (každá změna ceny), `assessments`
   a `poll_runs` (záznam o každém průchodu).

| Verdikt | Kdy | Ping |
|---|---|---|
| KOUPIT | cena ≤ reference × `buy_ratio` | ano |
| KOUPIT_S_RIZIKEM | totéž, ale prasklina, iCloud, MDM, na díly | ano |
| SLEDOVAT | cena ≤ reference × `watch_ratio` | ne |
| NEVYPLATI_SE | dražší | ne |
| NEZNAMY_MODEL | model není v tabulce | jen pod `unknown_model_alert_below_czk` |
| BEZ_CENY, MIMO | bez ceny, nebo ne MacBook M1+ | ne |

## Setup od nuly

Potřebuješ jen Python 3.13 nebo novější. Za běhu žádné další balíčky.

```
git clone <repo> ~/Projects/macwatch
cd ~/Projects/macwatch
cp config.example.json config.json
```

V `config.json` nastav:

1. `discord_webhook_url`: Discord, nastavení kanálu, Integrace, Webhooky, Nový webhook,
   Kopírovat URL. Místo configu jde použít proměnnou `MACWATCH_DISCORD_WEBHOOK`.
2. `pricing.reference`: referenční ceny. Klíč je `bazos|air|m1|256`
   (zdroj, rodina `air` nebo `pro`, čip `m1`, `m2 pro`, `m3 max`..., SSD v GB:
   128, 256, 512, 1024, 2048). Varianta bez SSD `bazos|air|m1` platí jako záloha.

Zkušební běh (nic neuloží, nic neodešle, vypíše nálezy):

```
python3 -m macwatch --dry-run
```

Jeden ostrý průchod: `python3 -m macwatch --once`. Jen migrace: `--migrate`.
Podrobné logy: `-v`.

## Facebook Marketplace

Facebook se čte bez přihlášení přes fork `regek/facebook-marketplace-rss` v `~/Projects/fb-mp-rss`
(větev `macwatch`, změny v `PATCHES.md`). Běží v Dockeru a feed vystavuje jen na `127.0.0.1:5000`.

```
git clone -b macwatch <fork> ~/Projects/fb-mp-rss
cd ~/Projects/fb-mp-rss
mkdir -p data && chmod 777 data          # kontejner zapisuje jako uživatel appuser
cp config.sample.json data/config.json   # a uprav podle níže
docker compose up -d --build
docker logs -f fb-mp-rss                 # "N ads on page, M new"
curl -s http://127.0.0.1:5000/rss | head
```

V `data/config.json` je pod `url_filters` adresa hledání. Na Facebooku nastav lokalitu, okruh
a hledání „macbook“, zkopíruj adresu a přidej `sortBy=creation_time_descend`
(nejnovější první) a `daysSinceListed=1` (posledních 24 hodin). Bez přihlášení Facebook
ukáže jen první várku kolem 24 inzerátů, proto na řazení záleží. Filtr titulku:
`"level1": ["macbook", "mac book"]`. Interval `refresh_interval_minutes` nech na 15.

Feed nemá popis inzerátu, posouzení z Facebooku stojí jen na titulku a baterii ani
červené vlajky z něj nepozná. Dokud nejsou reference `facebook|...`, použijí se ceny z Bazoše
a v upozornění je to napsané.

## Testy

```
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

Testy nechodí na síť, pokus o spojení test shodí. Vzorky jsou v `tests/fixtures/bazos/`
(skutečné stránky, jméno prodávajícího a telefony nahrazené). Ručně ověřené hodnoty
k detailům jsou v `expected.json`, nový vzorek = HTML soubor plus řádek v něm.

## Běh jako služba

```
systemctl --user link ~/Projects/macwatch/deploy/macwatch.service
systemctl --user enable --now macwatch.service
sudo loginctl enable-linger $USER     # běh i bez přihlášení a po restartu
journalctl --user -u macwatch -f      # logy
journalctl --user -u macwatch -p warning   # jen varování a chyby
```

Unit počítá s projektem v `~/Projects/macwatch`, jinak uprav cesty v `deploy/macwatch.service`.
Při zastavení služba dokončí rozběhnutý průchod.

## Databáze a migrace

Verzi schématu drží `PRAGMA user_version`. Migrace jsou `macwatch/migrations/NNNN_nazev.sql`,
čísluj souvisle. Každá běží v jedné transakci a před změnou existující databáze
se vedle ní vytvoří záloha `macwatch.db.bak-vN-datum`.

## Přidání dalšího druhu zboží

Rodiny, vzor čipu, červené vlajky, obchodníci a zápory jsou v `config.json`
v sekci `parsing`. Kód obsahuje jen obecné extraktory (cena, kapacity, procenta, cykly).

## Cizí komponenty

| Komponenta | Licence | Role |
|---|---|---|
| Python standardní knihovna | PSF | běh |
| pytest | MIT | jen testy |
| facebook-marketplace-rss (fork) | BSD-3-Clause | sběr z Marketplace, Docker |
| Firefox, geckodriver, Selenium | MPL-2.0, MPL-2.0, Apache-2.0 | uvnitř kontejneru fb-mp-rss |
