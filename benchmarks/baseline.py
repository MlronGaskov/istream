"""Benchmark-only HTTP full-range and repeatable-read keyset endpoints.

A keyset scan holds a read-only transaction, not a materialized copy of all rows.
An opaque cursor binds its snapshot, bounds, last key and page size. Completed
scans release their connection; abandoned scans expire and are rolled back.
"""
import asyncio
import os
import time
from uuid import uuid4

import asyncpg
from aiohttp import web

from istream.model import Bounds, Query, count_arg, serialize

QUERY = Query.parse(None)


async def rows(conn, bounds, last=None, limit=None):
    # Keep score bounds indexable even after PostgreSQL switches to a generic
    # prepared plan. Nullable OR guards would let repeated small-range reads
    # degrade to full-table scans, an avoidable handicap for both baselines.
    clauses, args = ['active'], []
    for key, direction, tie in ((bounds.lower, '<=', '>='),
                                (bounds.upper, '>=', '<'), (last, '<=', '>')):
        if key is None:
            continue
        offset = len(args)+1
        args.extend(key)
        strict = '<' if direction == '<=' else '>'
        clauses.append(f'(score {direction} ${offset} AND '
                       f'(score {strict} ${offset} OR id {tie} ${offset+1}))')
    args.append(limit)
    return await conn.fetch(
        'SELECT id,name,score FROM items WHERE '+ ' AND '.join(clauses)
        + f' ORDER BY score DESC,id ASC LIMIT ${len(args)}', *args)



async def full(request):
    bounds = Bounds.parse((await request.json())['actualRange'])
    async with request.app['pool'].acquire() as conn:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            result = await rows(conn, bounds)
    return web.json_response({'rows': serialize(result, QUERY)})


async def release(app, token):
    scan = app['scans'].pop(token, None)
    if scan:
        await scan['tx'].rollback()
        await app['pool'].release(scan['conn'])


async def page(request):
    body = await request.json()
    app = request.app
    token = body.get('cursor')
    if token is None:
        bounds = Bounds.parse(body['actualRange'])
        size = count_arg(body.get('pageSize'))
        if len(app['scans']) >= 8:
            raise web.HTTPServiceUnavailable(text='Too many open scans')
        conn = await app['pool'].acquire()
        tx = conn.transaction(isolation='repeatable_read', readonly=True)
        try:
            await tx.start()
        except BaseException:
            await app['pool'].release(conn)
            raise
        token = uuid4().hex
        app['scans'][token] = dict(conn=conn, tx=tx, bounds=bounds,
                                   size=size, last=None, expires=time.monotonic()+30)
    scan = app['scans'].get(token)
    if scan is None:
        raise web.HTTPGone(text='Snapshot cursor expired')
    try:
        result = await rows(scan['conn'], scan['bounds'], scan['last'], scan['size']+1)
        more = len(result) > scan['size']
        result = result[:scan['size']]
        if more:
            scan['last'] = (result[-1]['score'], result[-1]['id'])
            scan['expires'] = time.monotonic()+30
        else:
            await release(app, token)
        return web.json_response({'rows': serialize(result, QUERY),
                                  'cursor': token if more else None})
    except BaseException:
        await release(app, token)
        raise


async def cancel(request):
    await release(request.app, request.match_info['token'])
    return web.Response(status=204)


async def lifetime(app):
    app['pool'] = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=10)
    app['scans'] = {}

    async def expire():
        while True:
            await asyncio.sleep(1)
            for token, scan in list(app['scans'].items()):
                if scan['expires'] <= time.monotonic():
                    await release(app, token)
    task = asyncio.create_task(expire())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for token in list(app['scans']):
        await release(app, token)
    await app['pool'].close()


def create_app():
    app = web.Application()
    app.router.add_post('/range', full)
    app.router.add_post('/page', page)
    app.router.add_delete('/scan/{token}', cancel)
    app.cleanup_ctx.append(lifetime)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=8080, access_log=None)
