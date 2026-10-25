import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp
import asyncpg
import pytest

from client import TestClient as IstreamClient, sse_events
from main import reference
from state import Fragment

URL = os.environ["ISTREAM_URL"]
DSN = os.environ["DATABASE_URL"]


def generator(*args):
    subprocess.run([sys.executable, "/app/generator.py", *map(str, args)], check=True)


@pytest.fixture(scope="session", autouse=True)
def await_server():
    import urllib.request

    for _ in range(60):
        try:
            request = urllib.request.Request(
                f"{URL}/count",
                data=b'{"query": {"active": null}}',
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError):
            time.sleep(0.25)

    raise RuntimeError("ISTREAM server is unavailable")


@pytest.fixture(autouse=True)
def reset():
    generator("seed", "--rows", 20, "--seed", 42)


async def post(session, route, body):
    async with session.post(URL + route, json=body) as response:
        response.raise_for_status()
        return await response.json()


async def current_version(conn):
    return await conn.fetchval(
        "SELECT version FROM stream_clock WHERE id = 1"
    )


async def assert_reference(fragment):
    version, rows = await reference(DSN, fragment.bounds)
    assert fragment.applied_version == version
    assert fragment.ordered_rows() == rows


def test_fetch_and_navigation():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            first = await post(session, "/fetch/first", {"n": 5})

            assert len(first["rows"]) == 5
            assert first["actualRange"]["lower"] is None
            assert first["actualRange"]["upper"] is not None

            again = await post(session, "/fetch/range", {
                "contextId": first["contextId"],
                "actualRange": first["actualRange"],
            })

            assert first["rows"] == again["rows"]
            assert first["asOfVersion"] == again["asOfVersion"]

            boundary = first["actualRange"]["upper"]

            after = await post(session, "/fetch/after", {
                "contextId": first["contextId"],
                "key": boundary,
                "n": 3,
            })

            before = await post(session, "/fetch/before", {
                "contextId": first["contextId"],
                "key": boundary,
                "n": 5,
            })

            around = await post(session, "/fetch/around", {
                "contextId": first["contextId"],
                "key": boundary,
                "nBefore": 2,
                "nAfter": 2,
            })

            assert len(after["rows"]) == 3
            assert before["rows"] == first["rows"]
            assert around["rows"] == first["rows"][-2:] + after["rows"][:2]

            total = await post(session, "/count", {
                "query": {"active": None}
            })

            assert total["count"] == 20

            inactive = await post(session, "/fetch/first", {
                "query": {
                    "fields": ["name"],
                    "active": False
                },
                "n": 10,
            })

            assert len(inactive["rows"]) == 4
            assert all(
                set(row["row"]) == {"name"}
                for row in inactive["rows"]
            )

    asyncio.run(scenario())


def test_atomic_delta():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            snapshot = await post(session, "/fetch/first", {"n": 100})
            fragment = Fragment(snapshot)

            ids = [
                item["row"]["id"]
                for item in snapshot["rows"][:2]
            ]

            conn = await asyncpg.connect(DSN)

            try:
                async with conn.transaction():
                    for row_id in ids:
                        await conn.execute(
                            "UPDATE items SET name = name || '-modified' "
                            "WHERE id = $1",
                            row_id
                        )

                target = await current_version(conn)
            finally:
                await conn.close()

            params = {
                "contextId": fragment.context_id,
                "actualRange": json.dumps(fragment.bounds),
                "afterVersion": str(fragment.applied_version),
            }

            deltas = []

            async with asyncio.timeout(10):
                async with session.get(
                    URL + "/watch", params=params
                ) as response:
                    response.raise_for_status()

                    async for event, message in sse_events(response):
                        if event == "delta":
                            deltas.append(message)
                            fragment.apply_delta(message)

                        elif event == "progress":
                            fragment.apply_progress(message)

                            if fragment.applied_version >= target:
                                break

            assert len(deltas) == 1
            assert deltas[0]["version"] == target
            assert {
                op["id"]
                for op in deltas[0]["operations"]
            } == set(ids)
            assert {
                op["type"]
                for op in deltas[0]["operations"]
            } == {"update"}

            await assert_reference(fragment)

    asyncio.run(scenario())


def test_delta_types():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            snapshot = await post(session, "/fetch/first", {"n": 100})
            fragment = Fragment(snapshot)

            conn = await asyncpg.connect(DSN)

            try:
                await conn.execute(
                    "UPDATE items SET score = 20000 WHERE id = 1"
                )
                await conn.execute(
                    "UPDATE items SET active = TRUE WHERE id = 3"
                )
                await conn.execute(
                    "UPDATE items SET active = FALSE WHERE id = 1"
                )
                await conn.execute(
                    "UPDATE items SET name = 'changed' WHERE id = 2"
                )

                target = await current_version(conn)
            finally:
                await conn.close()

            params = {
                "contextId": fragment.context_id,
                "actualRange": json.dumps(fragment.bounds),
                "afterVersion": str(fragment.applied_version),
            }

            types = []

            async with asyncio.timeout(10):
                async with session.get(
                    URL + "/watch", params=params
                ) as response:
                    response.raise_for_status()

                    async for event, message in sse_events(response):
                        if event == "delta":
                            types.extend(
                                op["type"]
                                for op in message["operations"]
                            )
                            fragment.apply_delta(message)

                        elif event == "progress":
                            fragment.apply_progress(message)

                            if fragment.applied_version >= target:
                                break

            assert types == ["move", "insert", "delete", "update"]
            await assert_reference(fragment)

    asyncio.run(scenario())


def test_replay_between_fetch_and_watch():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = IstreamClient(session, URL)
            await client.bootstrap(5)

            row_id = client.fragment.ordered_rows()[0]["row"]["id"]

            conn = await asyncpg.connect(DSN)

            try:
                await conn.execute(
                    "UPDATE items SET name = 'replayed' WHERE id = $1",
                    row_id
                )
                await conn.execute(
                    "INSERT INTO items (id, name, score, active) "
                    "VALUES (1001, 'new', 20000, TRUE)"
                )
                await conn.execute(
                    "DELETE FROM items WHERE id = $1",
                    row_id
                )

                target = await current_version(conn)
            finally:
                await conn.close()

            result = await asyncio.wait_for(
                client.watch_once(target),
                timeout=10
            )

            assert result == "target-reached"
            await assert_reference(client.fragment)
            assert 1001 in client.fragment.records
            assert row_id not in client.fragment.records

    asyncio.run(scenario())


def test_reconnect_from_applied_version():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = IstreamClient(session, URL)
            await client.bootstrap(5)

            row_id = client.fragment.ordered_rows()[0]["row"]["id"]

            conn = await asyncpg.connect(DSN)

            try:
                await conn.execute(
                    "UPDATE items SET name = 'first' WHERE id = $1",
                    row_id
                )

                first_version = await current_version(conn)

                assert await asyncio.wait_for(
                    client.watch_once(first_version),
                    timeout=10
                ) == "target-reached"

                await conn.execute(
                    "UPDATE items SET name = 'second' WHERE id = $1",
                    row_id
                )

                second_version = await current_version(conn)

                assert await asyncio.wait_for(
                    client.watch_once(second_version),
                    timeout=10
                ) == "target-reached"

            finally:
                await conn.close()

            assert client.fragment.applied_version == second_version
            await assert_reference(client.fragment)

    asyncio.run(scenario())


def test_progress_without_range_changes():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = IstreamClient(session, URL)
            await client.bootstrap(3)

            before = client.fragment.ordered_rows()

            conn = await asyncpg.connect(DSN)

            try:
                await conn.execute(
                    "UPDATE items SET name = 'ignored' WHERE id = 3"
                )

                target = await current_version(conn)
            finally:
                await conn.close()

            assert await asyncio.wait_for(
                client.watch_once(target),
                timeout=10
            ) == "target-reached"

            assert client.fragment.ordered_rows() == before
            await assert_reference(client.fragment)

    asyncio.run(scenario())


def test_history_expired_refetch():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = IstreamClient(session, URL)
            await client.bootstrap(5)

            original_bounds = client.fragment.bounds

            conn = await asyncpg.connect(DSN)

            try:
                await conn.execute(
                    "INSERT INTO items (id, name, score, active) "
                    "VALUES (1001, 'new', 20000, TRUE)"
                )

                await conn.fetchval(
                    "SELECT stream_prune_history(0)"
                )
            finally:
                await conn.close()

            assert await asyncio.wait_for(
                client.watch_once(),
                timeout=10
            ) == "history-expired"

            await client.recover()

            assert client.fragment.bounds == original_bounds
            await assert_reference(client.fragment)

    asyncio.run(scenario())


def test_generator_end_to_end():
    async def scenario():
        async with aiohttp.ClientSession() as session:
            client = IstreamClient(session, URL)
            await client.bootstrap(10)

            with TemporaryDirectory() as temp:
                a = Path(temp) / "a.jsonl"
                b = Path(temp) / "b.jsonl"

                args = [
                    "plan",
                    "--initial-rows", 20,
                    "--count", 30,
                    "--interval-ms", 0,
                    "--seed", 42,
                ]

                generator(*args, "--output", a)
                generator(*args, "--output", b)

                assert a.read_bytes() == b.read_bytes()

                generator("run", "--input", a)

            conn = await asyncpg.connect(DSN)

            try:
                target = await current_version(conn)
            finally:
                await conn.close()

            assert await asyncio.wait_for(
                client.watch_once(target),
                timeout=10
            ) == "target-reached"

            await assert_reference(client.fragment)

    asyncio.run(scenario())
