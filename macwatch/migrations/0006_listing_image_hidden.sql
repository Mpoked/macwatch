-- Náhled fotky ze zdroje (Bazoš ho má odvozený z čísla inzerátu, tady je pro ostatní)
-- a skrytí inzerátu z přehledu jedním kliknutím.
ALTER TABLE listings ADD COLUMN image_url TEXT;
ALTER TABLE listings ADD COLUMN hidden_at TEXT;
