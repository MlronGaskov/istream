import asyncio
import json

import aiohttp

from state import Fragment, ProtocolError


class ContextExpired(RuntimeError):
    pass


async def sse_events(response):
    event_name = "message"
    data = []

    async for raw_line in response.content:
        line = raw_line.decode("utf-8").rstrip("\r\n")

        if not line:
            if data:
                yield event_name, json.loads("\n".join(data))

            event_name = "message"
            data = []

        elif line.startswith("event:"):
            event_name = line[6:].strip()

        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


class TestClient:
    def __init__(self, session, base_url):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.query = {
            "fields": ["id", "name", "score"],
            "active": True
        }
        self.fragment = None
        self.initial_size = None

    async def post(self, path, body):
        async with self.session.post(
            self.base_url + path,
            json=body
        ) as response:
            if response.status == 410:
                raise ContextExpired()

            response.raise_for_status()
            return await response.json()

    async def bootstrap(self, n=None):
        if n is not None:
            self.initial_size = n

        snapshot = await self.post("/fetch/first", {
            "query": self.query,
            "n": self.initial_size
        })

        self.fragment = Fragment(snapshot)

        print(
            f"fetch: rows={len(self.fragment.records)} "
            f"version={self.fragment.applied_version}"
        )

    async def recover(self):
        old = self.fragment

        try:
            snapshot = await self.post("/fetch/range", {
                "contextId": old.context_id,
                "actualRange": old.bounds
            })

            self.fragment = Fragment(snapshot)

            print(
                f"refetch: rows={len(self.fragment.records)} "
                f"version={self.fragment.applied_version}"
            )

        except ContextExpired:
            await self.bootstrap()

    async def watch_once(self, target=None):
        fragment = self.fragment

        params = {
            "contextId": fragment.context_id,
            "actualRange": json.dumps(
                fragment.bounds,
                separators=(",", ":")
            ),
            "afterVersion": str(fragment.applied_version)
        }

        async with self.session.get(
            self.base_url + "/watch",
            params=params
        ) as response:
            if response.status == 410:
                return "context-expired"

            response.raise_for_status()

            async for event, message in sse_events(response):
                if event == "subscribed":
                    fragment.range_id = message["rangeId"]
                    print(f"watch: rangeId={fragment.range_id}")
                    continue

                if event not in {"delta", "progress", "history-expired"}:
                    continue

                if (
                    fragment.range_id is None
                    or message["rangeId"] != fragment.range_id
                ):
                    raise ProtocolError("Unexpected rangeId")

                if event == "history-expired":
                    return "history-expired"

                if event == "delta":
                    fragment.apply_delta(message)

                    print(
                        f"delta: version={message['version']} "
                        f"operations={len(message['operations'])}"
                    )

                else:
                    previous = fragment.applied_version
                    fragment.apply_progress(message)

                    if fragment.applied_version != previous:
                        print(
                            f"progress: version={fragment.applied_version}"
                        )

                if (
                    target is not None
                    and fragment.applied_version >= target
                ):
                    return "target-reached"

        return "disconnected"

    async def follow(self, seconds, target=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds

        while loop.time() < deadline:
            if (
                target is not None
                and self.fragment.applied_version >= target
            ):
                return

            try:
                async with asyncio.timeout(
                    deadline - loop.time()
                ):
                    result = await self.watch_once(target)

                if result == "history-expired":
                    await self.recover()

                elif result == "context-expired":
                    await self.bootstrap()

                elif result == "target-reached":
                    return

            except TimeoutError:
                break

            except (aiohttp.ClientError, OSError) as error:
                print(f"reconnect: {error}")

            await asyncio.sleep(
                min(0.25, max(0.0, deadline - loop.time()))
            )

        if (
            target is not None
            and self.fragment.applied_version < target
        ):
            raise TimeoutError(f"Version {target} not reached")
