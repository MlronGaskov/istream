CREATE TABLE stream_history (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    floor_version BIGINT NOT NULL DEFAULT 0 CHECK (floor_version >= 0)
);

INSERT INTO stream_history (id, floor_version) VALUES (1, 0);

CREATE FUNCTION stream_prune_history(keep_last_versions BIGINT)
RETURNS BIGINT AS $$
DECLARE
    current_version BIGINT;
    target_floor BIGINT;
BEGIN
    IF keep_last_versions < 0 OR keep_last_versions IS NULL THEN
        RAISE EXCEPTION 'keep_last_versions must be non-negative';
    END IF;

    SELECT version INTO current_version FROM stream_clock WHERE id = 1 FOR UPDATE;

    UPDATE stream_history
    SET floor_version = GREATEST(floor_version, current_version - keep_last_versions)
    WHERE id = 1
    RETURNING floor_version INTO target_floor;

    DELETE FROM stream_events WHERE version <= target_floor;

    RETURN target_floor;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION stream_reject_id_change()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'Changing row id is not supported by this prototype';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stream_before_id_change
BEFORE UPDATE OF id ON items
FOR EACH ROW EXECUTE FUNCTION stream_reject_id_change();
