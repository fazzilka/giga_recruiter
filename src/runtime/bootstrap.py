import asyncio
import fcntl
import signal
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def process_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "process.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Программа уже запущена.") from None
        yield


def run_async_entrypoint(main):
    async def guarded():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            await main()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    try:
        asyncio.run(guarded())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
