from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rivet.authorization import AuthorizationError, authorize_source
from rivet.config import Settings
from rivet.db import Store
from rivet.media import detect_highlights, validate_srt
from rivet.publisher import dispatch


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "rivet.db")
        self.source = self.store.create_source({
            "id": "source-1", "name": "Owned recording",
            "source_url": "https://media.example.com/video.mp4", "allowed_host": "media.example.com",
            "ingest_authorized": True, "republish_authorized": True,
            "rights_note": "Owner supplied recording",
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_job_claim_is_atomic_state_change(self) -> None:
        job = self.store.create_job(self.source["id"], self.source["source_url"], None)
        claimed = self.store.claim_job()
        self.assertEqual(job["id"], claimed["id"])
        self.assertEqual("processing", claimed["status"])
        self.assertIsNone(self.store.claim_job())

    def test_publishing_requires_review_and_rights(self) -> None:
        job = self.store.create_job(self.source["id"], self.source["source_url"], None)
        clip = self.store.create_clip(job, 0, 30, 0.9, str(self.root / "clip.mp4"))
        destination = self.store.create_destination({
            "id": "manual", "kind": "manual_export", "display_name": "Owner upload", "enabled": True,
        })
        with self.assertRaises(ValueError):
            self.store.queue_publish(clip["id"], destination["id"], "Owner approval")
        approved = self.store.review_clip(clip["id"], "approved", "Looks good")
        item = self.store.queue_publish(approved["id"], destination["id"], "Owner approval")
        self.assertEqual("staged", item["status"])

    def test_manual_dispatch_stays_disabled_by_default(self) -> None:
        settings = Settings(self.root, self.root / "rivet.db", self.root, 0, "token", 100, 1, False)
        with self.assertRaises(PermissionError):
            dispatch(self.store, settings, {"id": "x", "clip_id": "x", "destination_id": "x", "approval_note": "x"})


class AuthorizationTests(unittest.TestCase):
    def test_exact_registered_url_is_required(self) -> None:
        source = {
            "ingest_authorized": 1, "source_url": "https://media.example.com/a.mp4",
            "allowed_host": "media.example.com",
        }
        authorize_source(source, source["source_url"], resolve_dns=False)
        with self.assertRaises(AuthorizationError):
            authorize_source(source, "https://media.example.com/b.mp4", resolve_dns=False)
        with self.assertRaises(AuthorizationError):
            authorize_source(source, "https://127.0.0.1/a.mp4", resolve_dns=False)


class MediaTests(unittest.TestCase):
    def test_caption_validation(self) -> None:
        validate_srt("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        with self.assertRaises(Exception):
            validate_srt("not captions")

    @patch("rivet.media.subprocess.run")
    def test_highlight_detection_uses_dense_audio_sections(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stderr = "silence_start: 50.0\nsilence_end: 65.0\n"
        candidates = detect_highlights(Path("source.mp4"), 120, limit=3)
        self.assertEqual(2, len(candidates))
        self.assertTrue(all(item.end - item.start >= 12 for item in candidates))


if __name__ == "__main__":
    unittest.main()

