import json
from collections import OrderedDict

from .model import key_of


async def read_batch(conn, cursor, batch_size):
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        state = await conn.fetchrow(
            "SELECT c.version, h.floor_version FROM stream_clock c CROSS JOIN stream_history h "
            "WHERE c.id = 1 AND h.id = 1"
        )
        if cursor < state["floor_version"]:
            return None, state["floor_version"], None
        versions = await conn.fetch(
            "SELECT DISTINCT version FROM stream_events "
            "WHERE version > $1 AND version <= $2 ORDER BY version LIMIT $3",
            cursor, state["version"], batch_size,
        )
        end = versions[-1]["version"] if versions else state["version"]
        rows = await conn.fetch(
            "SELECT version, event_id, row_id, old_row, new_row "
            "FROM stream_events WHERE version > $1 AND version <= $2 "
            "ORDER BY version, event_id",
            cursor, end,
        )
        return rows, end, state["version"]


def operations(records, query, bounds):
    changes = OrderedDict()
    for record in records:
        row_id = record["row_id"]
        old = json.loads(record["old_row"]) if record["old_row"] is not None else None
        new = json.loads(record["new_row"]) if record["new_row"] is not None else None
        if row_id not in changes:
            changes[row_id] = [old, new]
        else:
            changes[row_id][1] = new

    result = []
    for old, new in changes.values():
        was_inside = query.contains(old, bounds)
        is_inside = query.contains(new, bounds)
        if not was_inside and is_inside:
            result.append({
                "type": "insert", "id": new["id"],
                "newKey": list(key_of(new)), "row": query.project(new),
            })
        elif was_inside and not is_inside:
            result.append({
                "type": "delete", "id": old["id"],
                "oldKey": list(key_of(old)),
            })
        elif was_inside and is_inside:
            old_key, new_key = key_of(old), key_of(new)
            if old_key != new_key:
                result.append({
                    "type": "move", "id": new["id"],
                    "oldKey": list(old_key), "newKey": list(new_key),
                    "row": query.project(new),
                })
            elif query.project(old) != query.project(new):
                result.append({
                    "type": "update", "id": new["id"],
                    "key": list(new_key), "row": query.project(new),
                })
    return result
