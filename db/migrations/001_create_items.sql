CREATE TABLE IF NOT EXISTS items (
    id BIGINT PRIMARY KEY,
    name TEXT NOT NULL,
    score INTEGER NOT NULL,
    active BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_active_score
ON items (score DESC, id ASC)
WHERE active = true;
