from __future__ import annotations

import signal
import threading

from .api import build_server
from .config import Settings
from .db import Store
from .publisher import dispatch
from .worker import Worker
from .monitor import PlatformMonitor


def publisher_loop(settings: Settings, store: Store, stop: threading.Event) -> None:
    while not stop.wait(2):
        item = store.claim_publish()
        if not item:
            continue
        try:
            dispatch(store, settings, item)
        except Exception as exc:
            store.set_publish_status(item["id"], "failed", error=str(exc)[:2000])


def main() -> None:
    settings = Settings.from_env()
    settings.prepare()
    store = Store(settings.db_path)
    store.recover_interrupted_jobs()
    worker = Worker(settings, store)
    monitor = PlatformMonitor(settings, store)
    server = build_server(settings, store)
    server.monitor = monitor  # type: ignore[attr-defined]
    stop = threading.Event()
    threads = [
        threading.Thread(target=worker.run, name="rivet-worker", daemon=True),
        threading.Thread(target=monitor.run, name="rivet-monitor", daemon=True),
    ]
    if settings.publish_execution_enabled:
        threads.append(
            threading.Thread(target=publisher_loop, args=(settings, store, stop), name="rivet-publisher", daemon=True)
        )
    for thread in threads:
        thread.start()

    def shutdown(*_args) -> None:
        worker.stop()
        monitor.stop()
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        stop.set()
        worker.stop()
        monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()

