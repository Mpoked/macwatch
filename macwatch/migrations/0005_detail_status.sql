-- Stav stažení detailu u inzerátů přidaných z výpisu hledání:
-- NULL čeká na detail, 'skip' titulek model jasně vyřadil (Intel, starý rok), 'error' detail nešel přečíst.
ALTER TABLE listings ADD COLUMN detail_status TEXT;

CREATE INDEX listings_backlog ON listings (source, body_complete, detail_status, published_at);
