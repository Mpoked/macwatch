-- Cenová tabulka MacBooků (základ modelu + přirážky za RAM a SSD) a zásoba inzertních cen
-- pro její týdenní přepočet.

CREATE TABLE price_models (
    family          TEXT NOT NULL,
    chip            TEXT NOT NULL,
    base_ram_gb     INTEGER NOT NULL CHECK (base_ram_gb > 0),
    base_ssd_gb     INTEGER NOT NULL CHECK (base_ssd_gb > 0),
    base_price_czk  INTEGER NOT NULL CHECK (base_price_czk > 0),
    locked          INTEGER NOT NULL DEFAULT 0,
    samples         INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (family, chip)
);

CREATE TABLE asking_snapshots (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    title       TEXT NOT NULL,
    price_czk   INTEGER,
    eligible    INTEGER NOT NULL,
    family      TEXT,
    chip        TEXT,
    ram_gb      INTEGER,
    ssd_gb      INTEGER,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

CREATE INDEX asking_snapshots_seen ON asking_snapshots (last_seen);
