import argparse
import asyncio
import os

import aiohttp

from sparse import SparseClient


async def run(args):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = SparseClient(session, args.url)

        await client.bootstrap(args.ranges, args.rows, args.gap)

        for name, fragment in client.ordered():
            print(
                f"{name}: rows={len(fragment.records)} "
                f"version={fragment.applied_version} "
                f"range={fragment.bounds}"
            )

        print(f"gaps: {client.gaps()}")

        if args.disconnect_after is None:
            await client.follow(args.seconds)
        else:
            await client.follow(args.disconnect_after)

            print(f"offline: {args.offline_seconds}s")
            await asyncio.sleep(args.offline_seconds)

            await client.follow(
                args.seconds
                - args.disconnect_after
                - args.offline_seconds
            )

        if args.verify:
            version = await client.verify(args.dsn)

            print(
                f"verify: OK "
                f"fragments={len(client.fragments)} "
                f"version={version}"
            )
        else:
            for name, fragment in client.ordered():
                print(
                    f"{name}: rows={len(fragment.records)} "
                    f"version={fragment.applied_version}"
                )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--url",
        default=os.getenv("ISTREAM_URL", "http://localhost:8080"),
    )
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--ranges", type=int, default=3)
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--gap", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--disconnect-after", type=float)
    parser.add_argument("--offline-seconds", type=float, default=0)
    parser.add_argument("--verify", action="store_true")

    args = parser.parse_args()

    if (
        args.ranges < 1
        or args.rows < 1
        or not 0 <= args.gap < 1000
        or args.seconds <= 0
        or args.offline_seconds < 0
    ):
        parser.error("Invalid ranges, rows, gap or duration")

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
