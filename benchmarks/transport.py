"""Opt-in instrumentation only used by this benchmark."""
from collections import Counter, defaultdict
from contextlib import asynccontextmanager


class Content:
    def __init__(self, content, owner):
        self.content, self.owner = content, owner

    def __aiter__(self):
        return self.lines()

    async def lines(self):
        async for line in self.content:
            self.owner.add('bytes', len(line))
            yield line


class Response:
    def __init__(self, response, owner):
        self.response, self.owner = response, owner
        self.content = Content(response.content, owner)

    def __getattr__(self, name):
        return getattr(self.response, name)

    async def json(self):
        body = await self.response.read()
        self.owner.add('bytes', len(body))
        return await self.response.json()


class Meter:
    def __init__(self, session):
        self.session = session
        self.phase = 'initial'
        self.counts = defaultdict(Counter)

    def add(self, key, amount=1):
        self.counts[self.phase][key] += amount

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        self.add('requests')
        if url.endswith('/page'):
            self.add('pages')
        async with self.session.request(method, url, **kwargs) as response:
            yield Response(response, self)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)
