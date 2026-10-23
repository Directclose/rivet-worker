from __future__ import annotations

import argparse
import signal
import threading
import time
from pathlib import Path

from .authorization import authorize_source
from .config import Settings
from .db import Store
from .media import Candidate, detect_highlights, download, probe_duration, render_vertical, validate_srt


class Worker:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.running = True

    def stop(self, *_args) -> None:
        self.running = False

    def process(self, job: dict) -> None:
        source = self.store.get_source(job["source_id"])
        authorize_source(source, job["source_url"])
        job_dir = self.settings.artifacts_dir / job["id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        media_path = job_dir / "source"
        download(job["source_url"], media_path, self.settings.max_source_bytes)
        captions_path: Path | None = None
        if job.get("captions_srt"):
            validate_srt(job["captions_srt"])
            captions_path = job_dir / "captions.srt"
            captions_path.write_text(job["captions_srt"], encoding="utf-8")
        duration = probe_duration(media_path)
        candidates = detect_highlights(media_path, duration)
        if not candidates:
            raise RuntimeError("no viable highlight segments were detected")
        for index, candidate in enumerate(candidates, start=1):
            output = job_dir / f"clip-{index:02d}.mp4"
            render_vertical(media_path, output, candidate, captions_path)
            self.store.create_clip(job, candidate.start, candidate.end, candidate.score, str(output))
        self.store.set_job_status(job["id"], "pending_review")

    def run_once(self) -> bool:
        job = self.store.claim_job()
        if not job:
            return False
        try:
            self.process(job)
        except Exception as exc:  # Worker records the bounded failure and continues.
            self.store.set_job_status(job["id"], "failed", str(exc)[:2000])
        return True

    def run(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        while self.running:
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.prepare()
    worker = Worker(settings, Store(settings.db_path))
    if args.once:
        worker.run_once()
    else:
        worker.run()


if __name__ == "__main__":
    main()
