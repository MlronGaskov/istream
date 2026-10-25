import asyncio
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import aiohttp
import asyncpg
import pytest

from sparse import SparseClient

URL = os.environ["ISTREAM_URL"]
DSN = os.environ["DATABASE_URL"]


@pytest.fixture(scope="module", autouse=True)
def ready():
    for _ in range(60):
        try:
            req = Request(
                URL + "/count",
                data=b'{"query":{"active":null}}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)

    raise RuntimeError("ISTREAM server is unavailable")


@pytest.fixture(autouse=True)
def reset_data():
    subprocess.run(
        [
            sys.executable,
            "/app/generator.py",
            "seed",
            "--rows", "20",
            "--seed", "42",
        ],
        check=True,
    )


def test_sparse_sync_and_replay():
    async def scenario():
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None)
        ) as session:
            client = SparseClient(session, URL)
            await client.bootstrap(count=3, rows=3, gap=2)

            assert len(client.fragments) == 3
            assert len(client.gaps()) >= 2

            conn = await asyncpg.connect(DSN)

            try:
                moved_id = client.fragments["f3"].ordered_rows()[0]["key"][1]

                await conn.execute(
                    "UPDATE items SET score = 20000 WHERE id = $1",
                    moved_id,
                )

                await conn.execute(
                    "UPDATE items SET name = 'changed' WHERE id = 2"
                )

                version = await conn.fetchval(
                    "SELECT version FROM stream_clock WHERE id = 1"
                )
            finally:
                await conn.close()

            await client.follow(8, target=version)

            assert all(
                fragment.applied_version == version
                for fragment in client.fragments.values()
            )

            assert moved_id in client.fragments["f1"].records
            assert moved_id not in client.fragments["f3"].records

            assert await client.verify(DSN) == version

    asyncio.run(scenario())


def test_expired_history_recovers_only_one_fragment():
    async def scenario():
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None)
        ) as session:
            client = SparseClient(session, URL)
            await client.bootstrap(count=2, rows=3, gap=2)

            preserved = client.fragments["f1"]
            expired = client.fragments["f2"]

            conn = await asyncpg.connect(DSN)

            try:
                await conn.execute(
                    "UPDATE items SET name = 'new' WHERE id = 3"
                )

                version = await conn.fetchval(
                    "SELECT version FROM stream_clock WHERE id = 1"
                )

                await client.follow(
                    8,
                    target=version,
                    names=["f1"],
                )

                await conn.fetchval(
                    "SELECT stream_prune_history(0)"
                )
            finally:
                await conn.close()

            await client.follow(
                8,
                target=version,
                names=["f2"],
            )

            assert client.fragments["f1"] is preserved
            assert client.fragments["f2"] is not expired

            assert client.fragments["f1"].applied_version == version
            assert client.fragments["f2"].applied_version == version

            assert await client.verify(DSN) == version

    asyncio.run(scenario())
