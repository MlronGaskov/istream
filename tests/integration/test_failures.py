"""Failure/transaction checks. All waits are bounded; SQL is the independent oracle."""
import asyncio
import copy
import json

import aiohttp
import asyncpg
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from client import TestClient as SingleClient, sse_events
from sparse import SparseClient
from istream.app import create_app
from test_istream import DSN, URL, reset, await_server, current_version


async def collect(session, url, fragment, target, apply=True, after_version=None):
    result = []
    params = dict(contextId=fragment.context_id,
                  actualRange=json.dumps(fragment.bounds),
                  afterVersion=str(fragment.applied_version if after_version is None else after_version))
    async with asyncio.timeout(8):
        async with session.get(url + '/watch', params=params) as response:
            response.raise_for_status()
            async for event, message in sse_events(response):
                if event == 'history-expired':
                    return [(event, message)]
                if event == 'delta':
                    result.append((event, message))
                    if not apply:
                        return result
                    fragment.apply_delta(message)
                if event == 'progress':
                    result.append((event, message))
                    if apply:
                        fragment.apply_progress(message)
                    if message['watermark'] >= target:
                        return result


async def database_state(conn):
    return (await conn.fetch('SELECT * FROM items ORDER BY id'),
            await current_version(conn),
            await conn.fetch('SELECT * FROM stream_events ORDER BY event_id'))


def test_id_rejection_and_rollback():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = SparseClient(session, URL)
            await client.bootstrap(count=2)
            conn = await asyncpg.connect(DSN)
            try:
                # Check installed schema, not merely the migration file.
                assert await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE tgname='stream_before_id_change' AND NOT tgisinternal") == 1
                assert await conn.fetchval("SELECT to_regprocedure('stream_prune_history(bigint)') IS NOT NULL")
                before = await database_state(conn)
                with pytest.raises(asyncpg.RaiseError, match='Changing row id'):
                    async with conn.transaction():
                        await conn.execute('UPDATE items SET id=id+10000 WHERE id=1')
                assert await database_state(conn) == before
                transaction = conn.transaction()
                await transaction.start()
                await conn.execute("UPDATE items SET name='rolled-back', score=20000, active=NOT active WHERE id=1")
                await transaction.rollback()
                assert await database_state(conn) == before
                await conn.execute("UPDATE items SET name='committed', score=20001, active=TRUE WHERE id=1")
                target = await current_version(conn)
                # Exactly the committed transaction is delivered; aborted records are absent.
                events = await collect(session, URL, client.fragments['f1'], target)
                assert [m['version'] for e, m in events if e == 'delta'] == [target]
                assert all('rolled-back' not in json.dumps(m) for _, m in events)
                await client.follow(8, target=target)
                assert await client.verify(DSN) == target
            finally:
                await conn.close()
    asyncio.run(scenario())


def test_mixed_versions_and_multiple_reconnects():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = SparseClient(session, URL)
            await client.bootstrap(count=3)
            original = dict(client.fragments)
            moved = client.fragments['f3'].ordered_rows()[0]['key'][1]
            conn = await asyncpg.connect(DSN)
            try:
                await conn.execute('UPDATE items SET score=20000 WHERE id=$1', moved)
                v1 = await current_version(conn)
                await client.follow(8, target=v1, names=['f1'])
                assert moved in client.fragments['f1'].records
                assert moved in client.fragments['f3'].records
                assert client.fragments['f1'].applied_version > client.fragments['f3'].applied_version
                client.validate_ranges()  # mixed-version duplicate is permitted
                await conn.execute("UPDATE items SET name=name || '-offline'")
                v2 = await current_version(conn)
                await client.follow(8, target=v2, names=['f2'])
                await client.follow(8, target=v2)
                assert all(client.fragments[n] is f for n, f in original.items())
                assert await client.verify(DSN) == v2
            finally:
                await conn.close()
    asyncio.run(scenario())


def test_received_unapplied_delta_is_replayed():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = SparseClient(session, URL)
            await client.bootstrap(count=1, rows=100)
            fragment = client.fragments['f1']
            before = copy.deepcopy(fragment.records)
            version = fragment.applied_version
            conn = await asyncpg.connect(DSN)
            try:
                await conn.execute("UPDATE items SET name=name || '-group' WHERE active")
                target = await current_version(conn)
                lost = await collect(session, URL, fragment, target, apply=False)
                assert fragment.records == before and fragment.applied_version == version
                replay = await collect(session, URL, fragment, target)
                assert [(m['version'], m['operations']) for e, m in lost if e == 'delta'] == [(m['version'], m['operations']) for e, m in replay if e == 'delta']
                after = copy.deepcopy(fragment.records)
                fragment.apply_delta(lost[0][1])
                assert fragment.records == after and fragment.applied_version == target
                assert await client.verify(DSN) == target
            finally:
                await conn.close()
    asyncio.run(scenario())


def test_retention_floor_equality_and_below():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = SparseClient(session, URL)
            await client.bootstrap(count=1, rows=100)
            fragment = client.fragments['f1']
            conn = await asyncpg.connect(DSN)
            try:
                await conn.execute("UPDATE items SET name='floor' WHERE id=1")
                floor = await conn.fetchval('SELECT stream_prune_history(0)')
                assert (await collect(session, URL, fragment, floor))[0][0] == 'history-expired'
                await conn.execute("UPDATE items SET name='after-floor' WHERE id=1")
                target = await current_version(conn)
                events = await collect(session, URL, fragment, target, apply=False, after_version=floor)
                assert events[0][0] == 'delta' and events[0][1]['version'] == target
            finally:
                await conn.close()
    asyncio.run(scenario())


async def assert_name_query_reference(conn, fragment, version):
    # Independent oracle for fields=[name], active=null; not SparseClient.verify.
    lower, upper = fragment.bounds['lower'], fragment.bounds['upper']
    rows = await conn.fetch(
        """SELECT id, name, score FROM items
        WHERE ($1::integer IS NULL OR score < $1 OR (score=$1 AND id >= $2))
          AND ($3::integer IS NULL OR score > $3 OR (score=$3 AND id < $4))
        ORDER BY score DESC, id ASC""",
        lower[0] if lower else None, lower[1] if lower else None,
        upper[0] if upper else None, upper[1] if upper else None,
    )
    assert fragment.applied_version == version
    assert fragment.ordered_rows() == [
        {'key': [r['score'], r['id']], 'row': {'name': r['name']}}
        for r in rows
    ]


def test_context_loss_preserves_query_and_bounds():
    async def scenario():
        # Same production app; emulate loss of its volatile context registry.
        async with TestServer(create_app()) as server:
            url = str(server.make_url('')).rstrip('/')
            async with aiohttp.ClientSession() as session:
                query = {'fields': ['name'], 'active': None}
                sparse = SparseClient(session, url)
                single = SingleClient(session, url)
                sparse.query = copy.deepcopy(query)
                single.query = copy.deepcopy(query)
                await sparse.bootstrap(count=2)
                await single.bootstrap(4)
                bounds = {n: copy.deepcopy(f.bounds) for n, f in sparse.fragments.items()}
                single_bounds = copy.deepcopy(single.fragment.bounds)
                old = sparse.context_id
                server.app['contexts'].queries.clear()
                conn = await asyncpg.connect(DSN)
                try:
                    await conn.execute("INSERT INTO items VALUES (10001, 'new-head', 30000, TRUE)")
                    target = await current_version(conn)
                    await sparse.follow(8, target=target)
                    await single.follow(8, target=target)
                    assert sparse.context_id != old
                    assert {n: f.bounds for n, f in sparse.fragments.items()} == bounds
                    assert single.fragment.bounds == single_bounds
                    fragments = [*sparse.fragments.values(), single.fragment]
                    for fragment in fragments:
                        await assert_name_query_reference(conn, fragment, target)
                    # Snapshots can already be at target. A further relevant write must
                    # actually arrive through Watch on the restored contexts.
                    identities = list(fragments)
                    await conn.execute("UPDATE items SET name=name || '-after-recovery'")
                    target = await current_version(conn)
                    await sparse.follow(8, target=target)
                    await single.follow(8, target=target)
                    assert all(sparse.fragments[n] is f for n, f in zip(sparse.fragments, identities))
                    assert single.fragment is identities[-1]
                    for fragment in identities:
                        assert fragment.range_id is not None
                        await assert_name_query_reference(conn, fragment, target)
                finally:
                    await conn.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['single', 'sparse'])
@pytest.mark.parametrize('failure', ['once', 'always', 'stalled'])
def test_repeated_context_loss_retries_with_deadline(kind, failure):
    async def scenario():
        armed = False
        attempts = 0
        range_requests = 0
        failures = 0
        original = None
        client = None

        def assert_preserved():
            if kind == 'single':
                assert client.fragment is original
            else:
                assert client.context_id == old_context
                assert client.fragments is original
                assert all(client.fragments[n] is f for n, f in original.items())

        @web.middleware
        async def lose_context(request, handler):
            nonlocal attempts, range_requests, failures
            if armed and request.path == '/fetch/first':
                attempts += 1
            if armed and request.path == '/fetch/range':
                range_requests += 1
                # Fail on the second sparse fragment: first was built privately,
                # but no partial replacement may have become visible.
                fail_at = 2 if kind == 'sparse' else 1
                if range_requests >= fail_at and (failure != 'once' or failures == 0):
                    assert_preserved()
                    failures += 1
                    if failure == 'stalled':
                        # Longer than follow's budget, but bounded even server-side.
                        await asyncio.sleep(1)
                    request.app['contexts'].queries.clear()
            return await handler(request)

        app = create_app()
        app.middlewares.append(lose_context)
        async with TestServer(app) as server:
            url = str(server.make_url('')).rstrip('/')
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
                client = SingleClient(session, url) if kind == 'single' else SparseClient(session, url)
                if kind == 'single':
                    await client.bootstrap(4)
                    original = client.fragment
                    old_context = original.context_id
                    bounds = copy.deepcopy(original.bounds)
                else:
                    await client.bootstrap(count=2)
                    original = client.fragments
                    old_context = client.context_id
                    bounds = {n: copy.deepcopy(f.bounds) for n, f in original.items()}
                app['contexts'].queries.clear()
                armed = True
                conn = await asyncpg.connect(DSN)
                try:
                    await conn.execute("UPDATE items SET name=name || '-retry'")
                    target = await current_version(conn)
                    if failure == 'once':
                        await asyncio.wait_for(client.follow(8, target=target), 9)
                        assert failures == 1 and attempts >= 2
                        fragments = [client.fragment] if kind == 'single' else list(client.fragments.values())
                        assert (client.fragment.bounds if kind == 'single' else {n:f.bounds for n,f in client.fragments.items()}) == bounds
                        if kind == 'single':
                            from test_istream import assert_reference
                            await assert_reference(client.fragment)
                        else:
                            assert await client.verify(DSN) == target
                        assert all(f.applied_version == target for f in fragments)
                    else:
                        # Outer guard catches a broken deadline without hanging the suite.
                        start = asyncio.get_running_loop().time()
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(client.follow(.6, target=target), 2)
                        assert asyncio.get_running_loop().time() - start < 1.5
                        assert failures >= 1
                        if failure == 'always':
                            assert attempts >= 2
                        assert_preserved()
                finally:
                    await conn.close()
    asyncio.run(scenario())


def test_concurrent_writers_commit_groups_and_progress():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = SparseClient(session, URL)
            await client.bootstrap(count=1, rows=100)
            fragment = client.fragments['f1']
            a, b, observer = await asyncio.gather(*(asyncpg.connect(DSN) for _ in range(3)))
            task = None
            try:
                await a.execute('BEGIN')
                await a.execute("UPDATE items SET name='writer-a' WHERE id IN (1,2)")
                av = await current_version(a)
                baseline = await current_version(observer)
                assert av > baseline
                async def writer_b():
                    async with b.transaction():
                        await b.execute("SET LOCAL statement_timeout='5s'")
                        await b.execute("UPDATE items SET name='writer-b' WHERE id IN (4,5)")
                task = asyncio.create_task(writer_b())
                # Wait for an observed lock wait, not a timing assumption.
                async with asyncio.timeout(3):
                    while not await observer.fetchval("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1", b.get_server_pid()):
                        await asyncio.sleep(.01)
                assert not task.done()
                assert await current_version(observer) == baseline
                assert await observer.fetchval('SELECT count(*) FROM stream_events WHERE version>$1', baseline) == 0
                snapshot = await client.post('/fetch/range', {'contextId':client.context_id, 'actualRange':fragment.bounds})
                assert snapshot['rows'] == fragment.ordered_rows()
                await a.execute('COMMIT')
                await asyncio.wait_for(task, 6)
                target = await current_version(observer)
                events = await collect(session, URL, fragment, target)
                deltas = [m for e,m in events if e=='delta']
                assert [m['version'] for m in deltas] == [av, target]
                assert [{op['id'] for op in m['operations']} for m in deltas] == [{1,2},{4,5}]
                assert events[-1][0] == 'progress' and events[-1][1]['watermark'] == target
                assert await client.verify(DSN) == target
            finally:
                await a.execute('ROLLBACK')
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await asyncio.gather(a.close(), b.close(), observer.close())
    asyncio.run(scenario())
