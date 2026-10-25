import argparse
import asyncio
import os

import aiohttp
import asyncpg

from client import TestClient


async def reference(dsn, bounds):
    connection = await asyncpg.connect(dsn)

    try:
        async with connection.transaction(
            isolation="repeatable_read",
            readonly=True
        ):
            version = await connection.fetchval(
                "SELECT version FROM stream_clock WHERE id = 1"
            )

            records = await connection.fetch(
                """
                SELECT id, name, score
                FROM items
                WHERE active = TRUE
                  AND (
                      $1::integer IS NULL
                      OR score < $1
                      OR (score = $1 AND id >= $2)
                  )
                  AND (
                      $3::integer IS NULL
                      OR score > $3
                      OR (score = $3 AND id < $4)
                  )
                ORDER BY score DESC, id ASC
                """,
                bounds["lower"][0] if bounds["lower"] is not None else None,
                bounds["lower"][1] if bounds["lower"] is not None else None,
                bounds["upper"][0] if bounds["upper"] is not None else None,
                bounds["upper"][1] if bounds["upper"] is not None else None
            )

            expected = [
                {
                    "key": [row["score"], row["id"]],
                    "row": {
                        "id": row["id"],
                        "name": row["name"],
                        "score": row["score"]
                    }
                }
                for row in records
            ]

            return version, expected

    finally:
        await connection.close()


async def run(args):
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=10
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = TestClient(session, args.url)

        await client.bootstrap(args.rows)

        if args.disconnect_after is None:
            await client.follow(args.seconds)

        else:
            await client.follow(args.disconnect_after)

            print(f"offline: {args.offline_seconds}s")

            await asyncio.sleep(args.offline_seconds)

            remaining = (
                args.seconds
                - args.disconnect_after
                - args.offline_seconds
            )

            await client.follow(remaining)

        if args.verify:
            verified_bounds = client.fragment.bounds

            version, expected = await reference(
                args.dsn,
                verified_bounds
            )

            await client.follow(10, target=version)

            if client.fragment.bounds != verified_bounds:
                raise RuntimeError(
                    "Materialized range changed during verification"
                )

            if client.fragment.applied_version != version:
                raise RuntimeError(
                    "Database changed during verification"
                )

            actual = client.fragment.ordered_rows()

            if actual != expected:
                raise AssertionError(
                    f"Mismatch: actual={actual} expected={expected}"
                )

            print(
                f"verify: OK rows={len(actual)} version={version}"
            )

        else:
            print(
                f"finished: rows={len(client.fragment.records)} "
                f"version={client.fragment.applied_version}"
            )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--url",
        default=os.getenv("ISTREAM_URL", "http://localhost:8080")
    )

    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL")
    )

    parser.add_argument("--rows", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--disconnect-after", type=float)
    parser.add_argument("--offline-seconds", type=float, default=0)
    parser.add_argument("--verify", action="store_true")

    args = parser.parse_args()

    if args.rows < 1 or args.seconds <= 0 or args.offline_seconds < 0:
        parser.error("Invalid rows or duration")

    if args.disconnect_after is not None and not (
        0 < args.disconnect_after < args.seconds
        and args.disconnect_after + args.offline_seconds < args.seconds
    ):
        parser.error("Invalid disconnect window")

    if args.verify and not args.dsn:
        parser.error("DATABASE_URL is required for --verify")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
