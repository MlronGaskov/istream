"""Run separately from measurements: snapshot consistency across concurrent writes."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import aiohttp
from aiohttp.test_utils import TestServer
import asyncpg

from .baseline import create_app


def test_keyset_snapshot_and_full_range():
    async def scenario():
        admin = await asyncpg.connect(os.environ['ADMIN_DSN'])
        assert await admin.fetchval('SELECT current_database()') == 'benchmark'
        db = 'istream_pilot_check_'+uuid4().hex[:10]
        await admin.execute(f'CREATE DATABASE {db}')
        dsn = os.environ['ADMIN_DSN'].rsplit('/',1)[0]+'/'+db
        os.environ['DATABASE_URL'] = dsn
        conn = await asyncpg.connect(dsn)
        await conn.execute(Path('/app/migrations/001_create_items.sql').read_text())
        await conn.execute("INSERT INTO items SELECT i,'item-'||i,100-i/2,TRUE FROM generate_series(1,100) i")
        bounds = {'lower':[95,10], 'upper':[60,80]}
        async def expected():
            records = await conn.fetch("""SELECT id,name,score FROM items WHERE active
                AND (score<95 OR (score=95 AND id>=10))
                AND (score>60 OR (score=60 AND id<80)) ORDER BY score DESC,id ASC""")
            return [{'key':[r['score'],r['id']], 'row':dict(r)} for r in records]
        before = await expected()
        async with TestServer(create_app()) as server:
            async with aiohttp.ClientSession() as session:
                async def post(route, body):
                    async with session.post(server.make_url(route),json=body) as response:
                        response.raise_for_status()
                        return await response.json()
                page = await post('/page',{'actualRange':bounds,'pageSize':25})
                accumulated = list(page['rows'])
                assert page['cursor'] is not None
                async with conn.transaction():
                    await conn.execute('DELETE FROM items WHERE id=40')
                    await conn.execute('UPDATE items SET score=1 WHERE id=50')
                    await conn.execute('UPDATE items SET active=FALSE WHERE id=60')
                    await conn.execute("UPDATE items SET name='changed' WHERE id=70")
                    await conn.execute("INSERT INTO items VALUES (1001,'inserted',80,TRUE)")
                while page['cursor']:
                    page = await post('/page',{'cursor':page['cursor']})
                    accumulated.extend(page['rows'])
                assert accumulated == before
                assert not server.app['scans']
                current = await expected()
                assert current != before
                assert (await post('/range',{'actualRange':bounds}))['rows'] == current
                page = await post('/page',{'actualRange':bounds,'pageSize':25})
                accumulated = list(page['rows'])
                while page['cursor']:
                    page = await post('/page',{'cursor':page['cursor']})
                    accumulated.extend(page['rows'])
                assert accumulated == current
                # Explicitly abandoned scan releases its snapshot/connection.
                page = await post('/page',{'actualRange':bounds,'pageSize':1})
                async with session.delete(server.make_url('/scan/'+page['cursor'])) as response:
                    assert response.status == 204
                assert not server.app['scans']
        await conn.close()
        await admin.close()
    asyncio.run(scenario())
