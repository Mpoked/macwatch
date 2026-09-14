-- Výchozí schéma. Pozorovaná data (listings, price_history) jsou oddělená
-- od odvozených (assessments), aby šlo posouzení přepočítat bez ztráty dat.

CREATE TABLE listings (
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    price_czk       INTEGER,
    price_raw       TEXT NOT NULL,
    body            TEXT,
    body_complete   INTEGER NOT NULL DEFAULT 0,
    category        TEXT,
    locality        TEXT,
    postal_code     TEXT,
    published_at    TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    gone_at         TEXT,
    alerted_at      TEXT,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE price_history (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    price_czk       INTEGER,
    price_raw       TEXT NOT NULL,
    FOREIGN KEY (source, source_id) REFERENCES listings (source, source_id)
);

CREATE INDEX price_history_listing ON price_history (source, source_id, observed_at);

CREATE TABLE assessments (
    source                  TEXT NOT NULL,
    source_id               TEXT NOT NULL,
    parser_version          INTEGER NOT NULL,
    assessed_at             TEXT NOT NULL,
    eligible                INTEGER NOT NULL,
    family                  TEXT,
    chip                    TEXT,
    ram_gb                  INTEGER,
    ssd_gb                  INTEGER,
    year                    INTEGER,
    battery_pct             INTEGER,
    cycles                  INTEGER,
    is_dealer               INTEGER NOT NULL DEFAULT 0,
    red_flags               TEXT NOT NULL DEFAULT '[]',
    reference_czk           INTEGER,
    reference_key           TEXT,
    reference_origin        TEXT,
    adjusted_reference_czk  INTEGER,
    target_czk              INTEGER,
    est_profit_czk          INTEGER,
    roi                     REAL,
    score                   REAL,
    verdict                 TEXT NOT NULL,
    PRIMARY KEY (source, source_id),
    FOREIGN KEY (source, source_id) REFERENCES listings (source, source_id)
);

CREATE INDEX assessments_score ON assessments (verdict, score);

CREATE TABLE poll_runs (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    items_seen          INTEGER NOT NULL DEFAULT 0,
    items_new           INTEGER NOT NULL DEFAULT 0,
    details_fetched     INTEGER NOT NULL DEFAULT 0,
    alerts_sent         INTEGER NOT NULL DEFAULT 0,
    errors              INTEGER NOT NULL DEFAULT 0,
    error_text          TEXT
);
