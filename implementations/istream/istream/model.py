from dataclasses import dataclass
from uuid import uuid4

from aiohttp import web

FIELDS = ("id", "name", "score")


@dataclass(frozen=True)
class Query:
    fields: tuple[str, ...]
    active: bool | None

    @classmethod
    def parse(cls, value):
        if value is None:
            value = {}
        if not isinstance(value, dict) or set(value) - {"fields", "active"}:
            raise web.HTTPBadRequest(text="Invalid query")
        fields = value.get("fields", list(FIELDS))
        active = value.get("active", True)
        if (not isinstance(fields, list) or not fields or
                any(type(f) is not str or f not in FIELDS for f in fields) or
                len(fields) != len(set(fields)) or
                (active is not None and type(active) is not bool)):
            raise web.HTTPBadRequest(text="Invalid query")
        return cls(tuple(fields), active)

    def project(self, row):
        return {field: row[field] for field in self.fields}

    def contains(self, row, bounds):
        return (row is not None and
                (self.active is None or row["active"] == self.active) and
                bounds.contains(key_of(row)))


@dataclass(frozen=True)
class Bounds:
    lower: tuple[int, int] | None
    upper: tuple[int, int] | None

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {"lower", "upper"}:
            raise web.HTTPBadRequest(text="Invalid actualRange")
        lower = parse_key(value["lower"], allow_none=True)
        upper = parse_key(value["upper"], allow_none=True)
        if lower is not None and upper is not None and not precedes(lower, upper):
            raise web.HTTPBadRequest(text="Invalid actualRange")
        return cls(lower, upper)

    def contains(self, key):
        return (self.lower is None or not precedes(key, self.lower)) and (
            self.upper is None or precedes(key, self.upper))

    def json(self):
        return {
            "lower": list(self.lower) if self.lower is not None else None,
            "upper": list(self.upper) if self.upper is not None else None,
        }


def parse_key(value, allow_none=False):
    if value is None and allow_none:
        return None
    if (not isinstance(value, list) or len(value) != 2 or
            type(value[0]) is not int or type(value[1]) is not int or
            not -(2**31) <= value[0] < 2**31 or
            not -(2**63) <= value[1] < 2**63):
        raise web.HTTPBadRequest(text="Invalid key")
    return tuple(value)


def precedes(a, b):
    return (-a[0], a[1]) < (-b[0], b[1])


def key_of(row):
    return (row["score"], row["id"])


def serialize(rows, query):
    return [{"row": query.project(row), "key": list(key_of(row))} for row in rows]


def count_arg(value, name="n", allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 1000:
        raise web.HTTPBadRequest(text=f"Invalid {name}")
    return value


class Contexts:
    def __init__(self):
        self.queries = {}

    def create(self, query):
        context_id = uuid4().hex
        self.queries[context_id] = query
        return context_id

    def get(self, context_id):
        if not isinstance(context_id, str) or context_id not in self.queries:
            raise web.HTTPGone(text="context-expired")
        return self.queries[context_id]

    def resolve(self, body):
        if "contextId" in body:
            return self.get(body["contextId"]), body["contextId"]
        query = Query.parse(body.get("query"))
        return query, None
