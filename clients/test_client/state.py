class ProtocolError(RuntimeError):
    pass


def sort_key(key):
    return (-key[0], key[1])


def in_range(key, bounds):
    position = sort_key(key)
    lower = bounds["lower"]
    upper = bounds["upper"]
    return (lower is None or position >= sort_key(lower)) and (
        upper is None or position < sort_key(upper)
    )


class Fragment:
    def __init__(self, snapshot):
        self.context_id = snapshot["contextId"]
        self.bounds = snapshot["actualRange"]
        self.applied_version = snapshot["asOfVersion"]
        self.range_id = None
        self.records = {entry["key"][1]: entry for entry in snapshot["rows"]}

        if len(self.records) != len(snapshot["rows"]):
            raise ProtocolError("Duplicate row IDs in snapshot")

        self.validate(self.records)

    def ordered_rows(self):
        return sorted(
            self.records.values(),
            key=lambda entry: sort_key(entry["key"])
        )

    def validate(self, records):
        for row_id, entry in records.items():
            key = entry["key"]

            if key[1] != row_id or entry["row"].get("id", row_id) != row_id:
                raise ProtocolError("Invalid row identity")

            if entry["row"].get("score", key[0]) != key[0]:
                raise ProtocolError("Inconsistent sorting key")

            if not in_range(key, self.bounds):
                raise ProtocolError("Row outside materialized range")

    def apply_delta(self, message):
        version = message["version"]

        if version <= self.applied_version:
            return

        updated = dict(self.records)

        for operation in message["operations"]:
            kind = operation["type"]
            row_id = operation["id"]
            existing = updated.get(row_id)

            if kind == "insert":
                if existing is not None:
                    raise ProtocolError("Duplicate insert")

                updated[row_id] = {
                    "key": operation["newKey"],
                    "row": operation["row"]
                }

            elif kind == "update":
                if existing is None or existing["key"] != operation["key"]:
                    raise ProtocolError("Invalid update")

                updated[row_id] = {
                    "key": operation["key"],
                    "row": operation["row"]
                }

            elif kind == "delete":
                if existing is None or existing["key"] != operation["oldKey"]:
                    raise ProtocolError("Invalid delete")

                del updated[row_id]

            elif kind == "move":
                if existing is None or existing["key"] != operation["oldKey"]:
                    raise ProtocolError("Invalid move")

                updated[row_id] = {
                    "key": operation["newKey"],
                    "row": operation["row"]
                }

            else:
                raise ProtocolError(f"Unknown operation: {kind}")

        self.validate(updated)
        self.records = updated
        self.applied_version = version

    def apply_progress(self, message):
        if message["watermark"] > self.applied_version:
            self.applied_version = message["watermark"]
