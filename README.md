# macwatch

Hlídá bazary s použitými MacBooky (Air a Pro s čipem M1 a novějším), posoudí cenu
proti referenčním cenám, výhodné kusy pošle na Discord a vede sklad a ekonomiku přeprodeje.

Zdroje: Bazoš (RSS, detail a stránka hledání) a Facebook Marketplace (přes upravený fb-mp-rss
v Dockeru). Dashboard běží na standardní knihovně Pythonu, bez build kroku.

## Co to umí

| Fáze | Co | Kde |
|---|---|---|
| A | Sběr z Bazoše a Marketplace, parser konfigurace, verdikt, Discord | `sources/`, `parsing.py`, `pricing.py`, `notify.py` |
| B | Životní cyklus: změny ceny, zmizelé inzeráty, doba prodeje, naměřené referenční ceny | `lifecycle.py`, `market.py` |
| C | Sklad: nákupy, díly, opravy, cesta, marže, ROI, roční ROI, strop kapitálu | `inventory.py`, dashboard Sklad |
| D | Vzdálenost z PSČ offline, cena cesty podle spotřeby a ceny paliva | `geo.py`, `data/cz_postal.tsv` |
| E | Webový dashboard, všechno nastavení včetně webhooku | `web/` |

### Jak se posuzuje

1. RSS hledání „macbook“ na Bazoši pokryje zhruba 7 hodin inzerátů, čte se každé 4 minuty.
   Facebook dodává fb-mp-rss každých 15 minut.
2. U nových MacBooků z Bazoše se stáhne detail s plným textem a PSČ. Po jednom, s rozestupem 4 s.
3. `privacy.py` nahradí telefony a e‑maily zástupkou. Jméno prodávajícího se nečte.
4. `parsing.py` vytáhne rodinu, čip, RAM, SSD, rok, baterii, cykly, obchodníka a rizika se zápory.
5. Reference kusu = cena základní konfigurace modelu × přirážky za RAM a SSD
   (výchozí +18 % za každé zdvojnásobení RAM, +11 % za SSD). Základ modelu se bere v tomto pořadí:
   ručně uložený a zamčený řádek cenové tabulky, medián prodejů (inzeráty zmizelé do 14 dnů
   a vlastní prodeje s trojnásobnou váhou, přepočtené na základní konfiguraci, aspoň 3 vzorky),
   cenová tabulka odhadnutá z inzertních cen, starší ruční ceny s klíčem `zdroj|rodina|čip|ssd`.
   Cenová tabulka se každých 7 dní přepočítá z inzertních cen, které nasbírá kontrola
   životního cyklu (`--refit-prices` ručně). Zamčené řádky přepočet nepřepíše.
6. Pingne se jen inzerát mladší než `alert_max_age_hours` (výchozí 3 h), počítáno od vložení
   nebo od posledního zlevnění. Starý výhodný kus zůstane v přehledu, ale nepřijde na Discord.
   V upozornění je napsané, kdy byl kus vložen nebo zlevněn.
7. K ceně se připočte cesta: vzdálenost od domova po silnici krát spotřeba krát cena paliva,
   tam i zpět. Cesta sníží zisk, ROI i verdikt. Kus dál než `max_km` se nepingne.

| Verdikt | Kdy (cena včetně cesty) | Ping |
|---|---|---|
| KOUPIT | ≤ reference × `buy_ratio`, nebo zisk ≥ `buy_min_profit_czk` | ano |
| KOUPIT_S_RIZIKEM | totéž, ale prasklina, iCloud, MDM, na díly | ano |
| SLEDOVAT | ≤ reference × `watch_ratio` | ne |
| NEVYPLATI_SE | dražší | ne |
| NEZNAMY_MODEL | model není v tabulce | jen pod `unknown_model_alert_below_czk` |
| BEZ_CENY, MIMO | bez ceny, nebo ne MacBook M1+ | ne |

### Životní cyklus (fáze B)

Každé 3 hodiny služba projde všechny stránky hledání na Bazoši (asi 75 stránek, 5 minut).
Známé inzeráty označí jako viděné a zapíše změnu ceny, zlevnění může spustit upozornění.
MacBooky, které v RSS nebyly (třeba vložené před spuštěním sběru nebo s titulkem bez čipu),
přidá do sledování s datem vložení z výpisu. Jejich detaily služba dostahuje postupně, nejnovější
první, ze zbytku limitu detailů na průchod. Titulky jasně vyřazené (Intel, starý rok) detail nestahují.
Staré inzeráty se objeví v přehledu, ale nepingnou (limit stáří pro upozornění).
Když průchod doběhne celý, neviděné aktivní inzeráty ověří přes detail: smazaný dostane `gone_at`.
Prodej = zmizení do `sold_within_days` od prvního spatření. Facebook bez přihlášení
zmizení nepozná, statistiky prodejů jsou zatím jen z Bazoše.

## Setup od nuly

Potřebuješ Python 3.13 nebo novější. Za běhu žádné další balíčky.

```
git clone https://github.com/Mpoked/macwatch.git ~/Projects/macwatch
cd ~/Projects/macwatch
cp config.example.json config.json
python3 -m macwatch --migrate
python3 -m macwatch --dry-run          # zkušební průchod, nic neuloží ani neodešle
```

Sběr z Facebooku je v repozitáři taky, v podadresáři `fb-mp-rss/`, nic dalšího se neklonuje.
Rozjede se jedním `docker compose` (viz Facebook Marketplace níže). Bez Dockeru běží Bazoš sám
o sobě, jen v `config.json` nech `sources.facebook.enabled` na `false`.

Všechno ostatní se nastavuje v dashboardu (sekce Nastavení a Ceny): Discord webhook,
feedy, poměry pro verdikt, domovské PSČ, max. vzdálenost nebo celá ČR, spotřeba,
cena paliva, strop kapitálu, režim referenčních cen. Nastavení se ukládá do databáze
a přebíjí `config.json`, služba ho načte při dalším průchodu bez restartu.
V `config.json` zůstávají jen cesta k databázi, HTTP a regulární výrazy parseru.

Další příkazy: `--once` jeden ostrý průchod, `--lifecycle` hned kontrola životního cyklu,
`--web` dashboard, `-v` podrobné logy.

## Běh jako služba

```
systemctl --user link ~/Projects/macwatch/deploy/macwatch.service
systemctl --user link ~/Projects/macwatch/deploy/macwatch-web.service
systemctl --user enable --now macwatch.service macwatch-web.service
sudo loginctl enable-linger $USER          # běh bez přihlášení a po restartu
journalctl --user -u macwatch -f           # logy sběru
journalctl --user -u macwatch -p warning   # jen varování a chyby
```

Unity počítají s projektem v `~/Projects/macwatch`. Dashboard poslouchá na `127.0.0.1:8080`.

### Dashboard z místní sítě

Výchozí unit poslouchá jen na 127.0.0.1. Pro přístup z jiného počítače v místní síti
(bez přihlášení, vidí ho každý v síti) přidej drop-in:

```
mkdir -p ~/.config/systemd/user/macwatch-web.service.d
cp deploy/macwatch-web-lan.conf ~/.config/systemd/user/macwatch-web.service.d/lan.conf
systemctl --user daemon-reload && systemctl --user restart macwatch-web
```

Server musí mít synchronizovaný čas (`timedatectl` ukáže `System clock synchronized: yes`),
jinak se ukládají chybné časy inzerátů.

## Dashboard za Cloudflare Access

Dashboard obsahuje obchodní data a nemá vlastní přihlašování. Nesmí být dostupný jinak než
přes Cloudflare Access.

1. V Zero Trust: Access, Applications, Add application, Self-hosted. Doména třeba
   `mac.tvoje-domena.cz`. Policy: Allow, Include, Emails, jen tvůj e‑mail.
2. Do konfigurace existujícího tunelu (`~/.cloudflared/config.yml` nebo v dashboardu tunelu)
   přidej pravidlo před závěrečné `http_status:404`:

   ```yaml
   ingress:
     - hostname: mac.tvoje-domena.cz
       service: http://127.0.0.1:8080
     # ... ostatní pravidla ...
     - service: http_status:404
   ```

3. `cloudflared tunnel route dns <tunel> mac.tvoje-domena.cz` a restart cloudflared.
4. Ověř v anonymním okně, že adresa nejdřív chce přihlášení přes Access.

Formuláře odmítají požadavky z cizích stránek (kontrola Origin proti Host, případně
X-Forwarded-Host). Když by za tunelem ukládání vracelo 403, nastav u pravidla
`originRequest: { httpHostHeader: mac.tvoje-domena.cz }`.

## Facebook Marketplace

Facebook se čte bez přihlášení přes upravený fork `regek/facebook-marketplace-rss`. Ten je
součástí tohohle repozitáře v podadresáři `fb-mp-rss/` (změny proti originálu popisuje
`fb-mp-rss/PATCHES.md`). Běží v Dockeru a feed vystavuje jen na `127.0.0.1:5000`.

```
cd ~/Projects/macwatch/fb-mp-rss
mkdir -p data && chmod 777 data          # kontejner zapisuje jako uživatel appuser
cp config.sample.json data/config.json   # a uprav podle níže
docker compose up -d --build
docker logs -f fb-mp-rss                 # "N ads on page, M new"
curl -s http://127.0.0.1:5000/rss | head
```

Adresář `fb-mp-rss/data/` je mimo git, drží běhový config, databázi feedu a log.

V `data/config.json` je pod `url_filters` adresa hledání. Na Facebooku nastav lokalitu, okruh
a hledání „macbook“, zkopíruj adresu a přidej `sortBy=creation_time_descend`
(nejnovější první) a `daysSinceListed=1` (posledních 24 hodin). Bez přihlášení Facebook
ukáže jen první várku kolem 24 inzerátů, proto na řazení záleží. Filtr titulku:
`"level1": ["macbook", "mac book"]`. Interval `refresh_interval_minutes` nech na 15.

Adresy hledání, klíčová slova, interval, stáří inzerátů a řazení se dají měnit v dashboardu
(Nastavení, sekce Facebook Marketplace). Dashboard zapíše `data/config.json` a fb-mp-rss ho
načte před další kontrolou bez restartu. Cestu k souboru určuje `sources.facebook.config_path`
v `config.json` macwatch, výchozí `fb-mp-rss/data/config.json` sedí na uložení v repozitáři.

fb-mp-rss čte data z JSONu, který Facebook vkládá do stránky. Ve feedu je proto skutečný čas
vložení, číselná cena, původní cena u zlevněných kusů a stav prodáno nebo rezervováno.
Prodaný kus se zapíše jako zmizelý s cenou (vzorek pro referenční ceny Marketplace),
rezervovaný se nepingne.

Feed nemá popis inzerátu ani PSČ, jen město. Posouzení stojí na titulku a vzdálenost se počítá
podle názvu obce.

## Sklad a ekonomika (fáze C)

Kus přidáš v dashboardu Sklad, nebo z detailu inzerátu tlačítkem „Koupil jsem“ (předvyplní
model, cenu, odkaz a ujeté km). U kusu evidueš díly, opravy a další náklady, vystavení a prodej.

* hrubá marže = prodej, minus nákup
* čistá marže = prodej, minus nákup, náklady, cesta (km × Kč/km z nastavení) a poplatky
* ROI = čistá marže / (nákup + náklady + cesta), roční ROI = (1 + ROI)^(365 / dní držení), minus 1
* vázaný kapitál = investice do neprodaných kusů, nad stropem dashboard varuje

Prodané kusy vstupují do referenčních cen s váhou `own_sale_weight` (výchozí 3).

## Testy

```
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

Testy nechodí na síť, pokus o spojení test shodí (výjimkou je jeden test dashboardu
na loopbacku). Vzorky jsou v `tests/fixtures/` (skutečné stránky, jména a telefony nahrazené).

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
| GeoNames postal codes CZ | CC BY 4.0 | PSČ a souřadnice, `macwatch/data/cz_postal.tsv` |
| facebook-marketplace-rss (fork) | BSD-3-Clause | sběr z Marketplace, Docker, `fb-mp-rss/` |
| Firefox, geckodriver, Selenium | MPL-2.0, MPL-2.0, Apache-2.0 | uvnitř kontejneru fb-mp-rss |
