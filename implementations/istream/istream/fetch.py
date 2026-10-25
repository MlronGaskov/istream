from aiohttp import web

from .model import Bounds, Query, count_arg, key_of, parse_key, serialize


async def payload(request):
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Invalid JSON")
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text="Expected an object")
    return value


async def read_rows(conn, query, bounds=Bounds(None, None), limit=None, reverse=False):
    order = "score ASC, id DESC" if reverse else "score DESC, id ASC"
    return await conn.fetch(
        f"""
        SELECT id, name, score, active FROM items
        WHERE ($1::boolean IS NULL OR active = $1)
          AND ($2::integer IS NULL OR score < $2 OR (score = $2 AND id >= $3))
          AND ($4::integer IS NULL OR score > $4 OR (score = $4 AND id < $5))
        ORDER BY {order} LIMIT $6
        """,
        query.active,
        bounds.lower[0] if bounds.lower is not None else None,
        bounds.lower[1] if bounds.lower is not None else None,
        bounds.upper[0] if bounds.upper is not None else None,
        bounds.upper[1] if bounds.upper is not None else None,
        limit,
    )


async def fetch(request):
    body = await payload(request)
    op = request.match_info["operation"]
    contexts = request.app["contexts"]
    query, context_id = contexts.resolve(body)

    if op in {"first", "after", "before"}:
        n = count_arg(body.get("n"))
    if op in {"after", "before", "around"}:
        key = parse_key(body.get("key"))
    if op == "around":
        before = count_arg(body.get("nBefore"), "nBefore", allow_zero=True)
        after = count_arg(body.get("nAfter"), "nAfter", allow_zero=True)
        if before + after == 0:
            raise web.HTTPBadRequest(text="Empty around window")
    if op == "range":
        if context_id is None:
            raise web.HTTPBadRequest(text="contextId required")
        bounds = Bounds.parse(body.get("actualRange"))

    async with request.app["pool"].acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            version = await conn.fetchval("SELECT version FROM stream_clock WHERE id = 1")
            if op == "first":
                fetched = await read_rows(conn, query, limit=n + 1)
                rows = fetched[:n]
                bounds = Bounds(None, key_of(fetched[n]) if len(fetched) > n else None)
            elif op == "after":
                fetched = await read_rows(conn, query, Bounds(key, None), limit=n + 1)
                rows = fetched[:n]
                bounds = Bounds(key, key_of(fetched[n]) if len(fetched) > n else None)
            elif op == "before":
                fetched = await read_rows(conn, query, Bounds(None, key), limit=n + 1, reverse=True)
                rows = list(reversed(fetched[:n]))
                bounds = Bounds(key_of(rows[0]) if len(fetched) >= n else None, key)
            elif op == "around":
                left = await read_rows(conn, query, Bounds(None, key), limit=before + 1, reverse=True) if before else []
                right = await read_rows(conn, query, Bounds(key, None), limit=after + 1) if after else []
                rows = list(reversed(left[:before])) + list(right[:after])
                lower = key if before == 0 else (key_of(rows[0]) if len(left) >= before else None)
                upper = key if after == 0 else (key_of(right[after]) if len(right) > after else None)
                bounds = Bounds(lower, upper)
            elif op == "range":
                rows = await read_rows(conn, query, bounds)
            else:
                raise web.HTTPNotFound()

    if context_id is None:
        context_id = contexts.create(query)
    return web.json_response({
        "rows": serialize(rows, query),
        "actualRange": bounds.json(),
        "asOfVersion": version,
        "contextId": context_id,
    })


async def count(request):
    body = await payload(request)
    query = Query.parse(body.get("query"))
    async with request.app["pool"].acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            version = await conn.fetchval("SELECT version FROM stream_clock WHERE id = 1")
            total = await conn.fetchval(
                "SELECT count(*) FROM items WHERE ($1::boolean IS NULL OR active = $1)",
                query.active,
            )
    return web.json_response({"count": total, "asOfVersion": version})
