CREATE TABLE stream_clock (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    version BIGINT NOT NULL
);

INSERT INTO stream_clock VALUES (1, 0);

CREATE TABLE stream_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version BIGINT NOT NULL,
    operation TEXT NOT NULL,
    row_id BIGINT NOT NULL,
    old_row JSONB,
    new_row JSONB
);

CREATE INDEX idx_stream_events_version
ON stream_events (version, event_id);

CREATE FUNCTION stream_next_version()
RETURNS BIGINT AS $$
DECLARE
    current_version TEXT;
    next_version BIGINT;
BEGIN
    current_version := current_setting('istream.version', TRUE);

    IF current_version IS NOT NULL AND current_version <> '' THEN
        RETURN current_version::BIGINT;
    END IF;

    UPDATE stream_clock
    SET version = version + 1
    WHERE id = 1
    RETURNING version INTO next_version;

    PERFORM set_config('istream.version', next_version::TEXT, TRUE);

    RETURN next_version;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION stream_prepare()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM stream_next_version();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION stream_capture()
RETURNS TRIGGER AS $$
DECLARE
    current_version BIGINT;
BEGIN
    current_version := current_setting('istream.version')::BIGINT;

    IF TG_OP = 'INSERT' THEN
        INSERT INTO stream_events
            (version, operation, row_id, new_row)
        VALUES
            (current_version, TG_OP, NEW.id, to_jsonb(NEW));

    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO stream_events
            (version, operation, row_id, old_row, new_row)
        VALUES
            (current_version, TG_OP, NEW.id, to_jsonb(OLD), to_jsonb(NEW));

    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO stream_events
            (version, operation, row_id, old_row)
        VALUES
            (current_version, TG_OP, OLD.id, to_jsonb(OLD));
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stream_before_change
BEFORE INSERT OR UPDATE OR DELETE ON items
FOR EACH STATEMENT
EXECUTE FUNCTION stream_prepare();

CREATE TRIGGER stream_after_change
AFTER INSERT OR UPDATE OR DELETE ON items
FOR EACH ROW
EXECUTE FUNCTION stream_capture();

CREATE FUNCTION stream_reject_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE is not supported on tracked tables';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stream_before_truncate
BEFORE TRUNCATE ON items
FOR EACH STATEMENT
EXECUTE FUNCTION stream_reject_truncate();
