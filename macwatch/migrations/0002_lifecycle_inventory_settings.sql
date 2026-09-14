-- Fáze B až E: životní cyklus, sklad, nastavení z webu, lokalita, shortlist.

-- B: kdy jsme inzerát naposled ověřili a proč zmizel
ALTER TABLE listings ADD COLUMN last_checked_at TEXT;
ALTER TABLE listings ADD COLUMN gone_reason TEXT;

CREATE INDEX listings_active ON listings (source, gone_at, last_seen);

-- D: vzdálenost a cena cesty jsou součást posouzení
ALTER TABLE assessments ADD COLUMN distance_km REAL;
ALTER TABLE assessments ADD COLUMN travel_cost_czk INTEGER;
ALTER TABLE assessments ADD COLUMN reference_samples INTEGER;

-- E: nastavení upravitelné z dashboardu, přebíjí config.json
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- E: ručně zadané referenční ceny (klíč zdroj|rodina|čip[|ssd])
CREATE TABLE reference_prices (
    key         TEXT PRIMARY KEY,
    price_czk   INTEGER NOT NULL CHECK (price_czk > 0),
    updated_at  TEXT NOT NULL
);

-- E: sledované kusy
CREATE TABLE shortlist (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    note        TEXT,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

-- C: sklad
CREATE TABLE inventory (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    family          TEXT,
    chip            TEXT,
    ram_gb          INTEGER,
    ssd_gb          INTEGER,
    bought_at       TEXT NOT NULL,
    buy_price_czk   INTEGER NOT NULL CHECK (buy_price_czk >= 0),
    bought_from     TEXT,
    listing_url     TEXT,
    source          TEXT,
    source_id       TEXT,
    travel_km       REAL,
    listed_at       TEXT,
    list_price_czk  INTEGER,
    sold_at         TEXT,
    sale_price_czk  INTEGER,
    sale_fees_czk   INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE inventory_costs (
    id              INTEGER PRIMARY KEY,
    inventory_id    INTEGER NOT NULL REFERENCES inventory (id) ON DELETE CASCADE,
    spent_at        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    description     TEXT,
    amount_czk      INTEGER NOT NULL
);

CREATE INDEX inventory_costs_item ON inventory_costs (inventory_id);
