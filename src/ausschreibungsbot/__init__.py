import asyncio


def main() -> None:
    from .app import run

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
