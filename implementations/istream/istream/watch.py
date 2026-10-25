import asyncio
import json
import time
from itertools import groupby
from uuid import uuid4

from aiohttp import web

from .journal import operations, read_batch
from .model import Bounds


async def send(response, name, data):
    value = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    await response.write(f"event: {name}\ndata: {value}\n\n".encode("utf-8"))


async def watch(request):
    params = request.query
    query = request.app["contexts"].get(params.get("contextId"))

    try:
        bounds = Bounds.parse(json.loads(params["actualRange"]))
        after_version = int(params["afterVersion"])
        if after_version < 0 or str(after_version) != params["afterVersion"]:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid Watch parameters")

    async with request.app["pool"].acquire() as conn:
        current = await conn.fetchval("SELECT version FROM stream_clock WHERE id = 1")
    if after_version > current:
        raise web.HTTPBadRequest(text="afterVersion is in the future")

    range_id = uuid4().hex
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    try:
        await send(response, "subscribed", {"rangeId": range_id})
        cursor = after_version
        last_write = time.monotonic()
        while True:
            async with request.app["pool"].acquire() as conn:
                records, end, watermark = await read_batch(conn, cursor, 128)

            if records is None:
                await send(response, "history-expired", {"rangeId": range_id})
                break

            for version, batch in groupby(records, key=lambda record: record["version"]):
                delta = operations(batch, query, bounds)
                if delta:
                    await send(response, "delta", {
                        "rangeId": range_id, "version": version,
                        "operations": delta,
                    })

            if end > cursor:
                await send(response, "progress", {
                    "rangeId": range_id, "watermark": end,
                })
                cursor = end
                last_write = time.monotonic()

            if end < watermark:
                continue
            if time.monotonic() - last_write >= 15:
                await response.write(b": keepalive\n\n")
                last_write = time.monotonic()
            await asyncio.sleep(0.25)
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    return response
