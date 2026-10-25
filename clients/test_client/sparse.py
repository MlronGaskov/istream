import asyncio
import json

import aiohttp
import asyncpg

from client import ContextExpired, sse_events
from state import Fragment, ProtocolError, sort_key


class SparseClient:
    def __init__(self, session, base_url):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.query = {"fields": ["id", "name", "score"], "active": True}
        self.context_id = None
        self.fragments = {}
        self.context_lock = asyncio.Lock()

    async def post(self, route, body):
        async with self.session.post(self.base_url + route, json=body) as response:
            if response.status == 410:
                raise ContextExpired()
            response.raise_for_status()
            return await response.json()

    def add(self, name, snapshot):
        fragment = Fragment(snapshot)

        if name in self.fragments or fragment.context_id != self.context_id:
            raise ProtocolError("Duplicate fragment or inconsistent context")

        self.fragments[name] = fragment

        try:
            self.validate_ranges()
        except Exception:
            del self.fragments[name]
            raise

        return fragment

    def ordered(self):
        return sorted(
            self.fragments.items(),
            key=lambda item: (0,) if item[1].bounds["lower"] is None
            else (1, *sort_key(item[1].bounds["lower"])),
        )

    def validate_ranges(self):
        ordered = self.ordered()

        for (_, previous), (_, current) in zip(ordered, ordered[1:]):
            upper = previous.bounds["upper"]
            lower = current.bounds["lower"]

            if upper is None or lower is None or sort_key(upper) > sort_key(lower):
                raise ProtocolError("Overlapping materialized ranges")

        for fragment in self.fragments.values():
            fragment.validate(fragment.records)

    def gaps(self):
        gaps = []
        cursor = None

        for _, fragment in self.ordered():
            lower = fragment.bounds["lower"]

            if lower is not None and (
                cursor is None or sort_key(cursor) < sort_key(lower)
            ):
                gaps.append({"lower": cursor, "upper": lower})

            cursor = fragment.bounds["upper"]

        if cursor is not None:
            gaps.append({"lower": cursor, "upper": None})

        return gaps

    async def bootstrap(self, count=2, rows=3, gap=2):
        if count < 1 or rows < 1 or gap < 0 or gap >= 1000:
            raise ValueError("Invalid window parameters")

        self.fragments = {}

        first = await self.post("/fetch/first", {
            "query": self.query,
            "n": rows,
        })

        self.context_id = first["contextId"]
        previous = self.add("f1", first)

        for index in range(2, count + 1):
            anchor = previous.bounds["upper"]

            if anchor is None:
                raise ValueError("Not enough rows to create disjoint ranges")

            if gap:
                skipped = await self.post("/fetch/after", {
                    "contextId": self.context_id,
                    "key": anchor,
                    "n": gap + 1,
                })

                if len(skipped["rows"]) <= gap:
                    raise ValueError("Not enough rows to skip")

                anchor = skipped["rows"][gap]["key"]

            snapshot = await self.post("/fetch/after", {
                "contextId": self.context_id,
                "key": anchor,
                "n": rows,
            })

            previous = self.add(f"f{index}", snapshot)

        self.validate_ranges()

    async def refetch(self, name):
        old = self.fragments[name]

        try:
            snapshot = await self.post("/fetch/range", {
                "contextId": old.context_id,
                "actualRange": old.bounds,
            })
        except ContextExpired:
            await self.reset_context(old.context_id)
            return

        replacement = Fragment(snapshot)

        if replacement.bounds != old.bounds:
            raise ProtocolError("Refetch changed materialized range")

        if self.fragments[name] is old:
            self.fragments[name] = replacement
            print(f"{name}: refetch version={replacement.applied_version}")

    async def reset_context(self, expired_id):
        async with self.context_lock:
            if expired_id != self.context_id:
                return

            first = await self.post("/fetch/first", {
                "query": self.query,
                "n": 1,
            })

            new_id = first["contextId"]
            updated = {}

            for name, fragment in self.fragments.items():
                snapshot = await self.post("/fetch/range", {
                    "contextId": new_id,
                    "actualRange": fragment.bounds,
                })

                updated[name] = Fragment(snapshot)

            self.fragments = updated
            self.context_id = new_id
            self.validate_ranges()

            print("context: recreated")

    async def follow_one(self, name, target, deadline):
        loop = asyncio.get_running_loop()

        while loop.time() < deadline:
            fragment = self.fragments[name]

            if target is not None and fragment.applied_version >= target:
                return

            params = {
                "contextId": fragment.context_id,
                "actualRange": json.dumps(
                    fragment.bounds, separators=(",", ":")
                ),
                "afterVersion": str(fragment.applied_version),
            }

            try:
                async with self.session.get(
                    self.base_url + "/watch", params=params
                ) as response:
                    if response.status == 410:
                        await self.reset_context(fragment.context_id)
                        continue

                    response.raise_for_status()

                    async for event, message in sse_events(response):
                        if self.fragments[name] is not fragment:
                            break

                        if event == "subscribed":
                            fragment.range_id = message["rangeId"]
                            continue

                        if event not in ("delta", "progress", "history-expired"):
                            continue

                        if (
                            fragment.range_id is None
                            or message["rangeId"] != fragment.range_id
                        ):
                            raise ProtocolError("Unexpected rangeId")

                        if event == "history-expired":
                            await self.refetch(name)
                            break

                        if event == "delta":
                            fragment.apply_delta(message)
                        else:
                            fragment.apply_progress(message)

                        if (
                            target is not None
                            and fragment.applied_version >= target
                        ):
                            return

            except (aiohttp.ClientError, OSError) as error:
                print(f"{name}: reconnect: {error}")

            await asyncio.sleep(
                min(0.25, max(0, deadline - loop.time()))
            )

        if (
            target is not None
            and self.fragments[name].applied_version < target
        ):
            raise TimeoutError(f"{name}: version {target} not reached")

    async def follow(self, seconds, target=None, names=None):
        selected = list(self.fragments) if names is None else list(names)

        if not selected:
            return

        for name in selected:
            if name not in self.fragments:
                raise KeyError(name)

        try:
            async with asyncio.timeout(seconds):
                deadline = asyncio.get_running_loop().time() + seconds

                await asyncio.gather(*(
                    self.follow_one(name, target, deadline)
                    for name in selected
                ))

        except TimeoutError:
            if target is not None:
                raise TimeoutError(
                    f"Not all ranges reached {target}"
                ) from None

        if target is not None and any(
            self.fragments[name].applied_version < target
            for name in selected
        ):
            raise TimeoutError(f"Not all ranges reached {target}")

    async def verify(self, dsn):
        connection = await asyncpg.connect(dsn)

        try:
            async with connection.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                version = await connection.fetchval(
                    "SELECT version FROM stream_clock WHERE id = 1"
                )

                expected = {}

                for name, fragment in self.fragments.items():
                    bounds = fragment.bounds

                    rows = await connection.fetch(
                        """
                        SELECT id, name, score FROM items
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
                        bounds["upper"][1] if bounds["upper"] is not None else None,
                    )

                    expected[name] = [
                        {
                            "key": [row["score"], row["id"]],
                            "row": {
                                "id": row["id"],
                                "name": row["name"],
                                "score": row["score"],
                            },
                        }
                        for row in rows
                    ]

            await self.follow(10, target=version)
            self.validate_ranges()

            if any(
                fragment.applied_version != version
                for fragment in self.fragments.values()
            ):
                raise RuntimeError("Database changed during verification")

            current = await connection.fetchval(
                "SELECT version FROM stream_clock WHERE id = 1"
            )

            if current != version:
                raise RuntimeError("Stop generator before verification")

            all_ids = []

            for name, fragment in self.fragments.items():
                if fragment.ordered_rows() != expected[name]:
                    raise AssertionError(
                        f"{name}: result differs from PostgreSQL"
                    )

                all_ids.extend(fragment.records)

            if len(set(all_ids)) != len(all_ids):
                raise AssertionError(
                    "Duplicate rowId across normalized fragments"
                )

            return version

        finally:
            await connection.close()
