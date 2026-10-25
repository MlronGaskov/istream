import os

import asyncpg
from aiohttp import web

from .fetch import count, fetch
from .model import Contexts
from .watch import watch


async def startup(app):
    app["pool"] = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=10)
    app["contexts"] = Contexts()


async def cleanup(app):
    await app["pool"].close()


def create_app():
    app = web.Application()
    app.router.add_post("/fetch/{operation:first|after|before|around|range}", fetch)
    app.router.add_post("/count", count)
    app.router.add_get("/watch", watch)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main():
    web.run_app(create_app(), host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
