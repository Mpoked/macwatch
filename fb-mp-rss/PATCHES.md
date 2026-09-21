# Úpravy oproti upstreamu

Fork `regek/facebook-marketplace-rss` (commit 719ef6e, prosinec 2024, licence BSD-3-Clause)
pro projekt macwatch. Ověřeno proti stránce Marketplace v září 2026 bez přihlášení.

| # | Problém v upstreamu | Úprava |
|---|---|---|
| 1 | Titulek hledá podle stylu `-webkit-line-clamp`, který na kartách už není. Za cenu bere první `span`, u nových inzerátů je to štítek „Just listed“. Výsledek: nevytáhne nic. | `extract_json_listings` čte strukturovaný JSON, který Facebook vkládá do stránky (`<script type="application/json">`). Když ho nenajde, `extract_cards` vezme texty karet v pořadí: štítky, cena, původní cena, titulek, lokalita. |
| 2 | Cena se testuje přes `startswith(currency)`, `9 000 Kč` neprojde. | Cena se pozná podle měny na začátku i na konci (`CZK15,000`, `9 000 Kč`). Klíč `currency` už není potřeba. |
| 3 | Neodmítne souhlas s cookies. | Klikne na „Decline optional cookies“ (i česká varianta). |
| 4 | Čeká na natvrdo zapsanou třídu `div.x78zum5...`, která se mění. | Čeká na odkaz `/marketplace/item/`, po 20 s vrátí stránku i tak. |
| 5 | První kontrola až po `refresh_interval_minutes`. | První kontrola hned po startu. |
| 6 | `guid` je MD5 z URL. | `guid` je číselné ID inzerátu z Facebooku. |
| 7 | Titulek a cena slepené v jednom poli, lokalita se zahazuje. | `title` je čistý titulek, `description` má řádky `price:`, `price_amount:`, `original_price:`, `location:`, `listed_at:` (čas vložení na Facebooku), `delivery:`, a jen když platí `is_sold: true` a `is_pending: true`. |
| 8 | `/rss` vrací jen položky od posledního dotazu, nepovedené čtení je ztratí. | `/rss` vrací inzeráty viděné za posledních 7 dní, nejnovější první. Duplicity řeší čtenář podle `guid`. |
| 9 | `pubDate` je vždy prázdné (`local_time` nic nevrací). | `pubDate` je čas prvního spatření. |
| 10 | Databáze leží v kontejneru a zmizí s ním. | Cesta z `DB_FILE`, výchozí v mountovaném `/app/config`. Chybějící sloupce se doplní. |
| 11 | geckodriver se stahuje z GitHubu při každé kontrole. | geckodriver 0.37.1 je v image. |
| 12 | Dočasné profily Firefoxu se nemažou (upstream issue „temp files are never purged“). | Po každé kontrole se smažou `rust_mozprofile*`. |
| 13 | `output.html` se přepisuje při každé kontrole. | Jen když je nastavené `SAVE_HTML=/cesta`. |
| 14 | Změna configu vyžaduje restart. | Před každou kontrolou se config načte znovu, pokud se změnil (adresy, filtry, interval, nejméně 15 min). Upravuje ho dashboard macwatch. |

## URL hledání

Nastav na Facebooku lokalitu, okruh a hledání a zkopíruj adresu. Pro řazení od nejnovějších
přidej `sortBy=creation_time_descend`, pro posledních 24 hodin `daysSinceListed=1`.
Bez přihlášení stránka ukáže jen první várku výsledků (kolem 24), proto je filtr
na poslední den a řazení od nejnovějších důležité.

## Testy

```
docker build -t fb-mp-rss:macwatch .
docker run --rm fb-mp-rss:macwatch python3 -m unittest discover -s tests
```
