from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from rivet.authorization import AuthorizationError, authorize_source
from rivet.config import Settings
from rivet.db import Store
from rivet.media import detect_highlights, probe_duration, validate_srt
from rivet.publisher import dispatch
from rivet.monitor import PlatformMonitor


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

    def test_interrupted_job_is_requeued_without_duplicate_clips(self) -> None:
        job = self.store.create_job(self.source["id"], self.source["source_url"], None)
        claimed = self.store.claim_job()
        self.store.create_clip(claimed, 0, 30, 0.9, str(self.root / "clip.mp4"))
        self.assertEqual([job["id"]], self.store.recover_interrupted_jobs())
        recovered = self.store.get_job(job["id"])
        self.assertEqual("queued", recovered["status"])
        self.assertEqual("requeued after worker restart", recovered["error"])
        self.assertEqual([], self.store.list_clips())
        self.assertEqual(job["id"], self.store.claim_job()["id"])

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
        self.assertEqual("staged", self.store.get_publish(item["id"])["status"])

    def test_manual_dispatch_stays_disabled_by_default(self) -> None:
        settings = Settings(self.root, self.root / "rivet.db", self.root, 0, "token", 100, 1, False)
        with self.assertRaises(PermissionError):
            dispatch(self.store, settings, {"id": "x", "clip_id": "x", "destination_id": "x", "approval_note": "x"})

    def test_core_roster_and_channel_candidates_are_persistent(self) -> None:
        creators = self.store.list_creators()
        self.assertEqual(19, len(creators))
        self.assertIn("Kai Cenat", {item["display_name"] for item in creators})
        channel = self.store.create_channel({
            "creator_id": "kai-cenat", "platform": "twitch", "platform_key": "kaicenat",
            "channel_url": "https://www.twitch.tv/kaicenat", "enabled": True,
        })
        first = self.store.upsert_candidate(channel["id"], {
            "platform": "twitch", "external_id": "stream-1", "title": "Live", "state": "live",
            "page_url": channel["channel_url"], "viewer_count": 100, "raw": {"id": "stream-1"},
        })
        second = self.store.upsert_candidate(channel["id"], {
            "platform": "twitch", "external_id": "stream-1", "title": "Still live", "state": "live",
            "page_url": channel["channel_url"], "viewer_count": 200, "raw": {"id": "stream-1"},
        })
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(200, self.store.list_candidates()[0]["viewer_count"])
        self.store.close_unseen_live_candidates("twitch", set())
        self.assertEqual("ended", self.store.list_candidates()[0]["state"])

    def test_authorized_candidate_can_queue_ingestion_only_once(self) -> None:
        channel = self.store.create_channel({
            "creator_id": "kai-cenat", "platform": "twitch", "platform_key": "kaicenat",
            "channel_url": "https://www.twitch.tv/kaicenat", "enabled": True,
        })
        candidate = self.store.upsert_candidate(channel["id"], {
            "platform": "twitch", "external_id": "stream-capture", "title": "Authorized archive",
            "state": "ended", "page_url": channel["channel_url"], "raw": {},
        })
        job = self.store.queue_candidate_ingestion(
            candidate["id"], "https://media.example.com/archive.mp4", "media.example.com",
            "Creator editor supplied this archive",
        )
        self.assertEqual("queued", job["status"])
        self.assertEqual(job["id"], self.store.get_candidate(candidate["id"])["ingestion_job_id"])
        self.assertEqual(0, self.store.get_source(job["source_id"])["republish_authorized"])
        with self.assertRaisesRegex(ValueError, "already has"):
            self.store.queue_candidate_ingestion(
                candidate["id"], "https://media.example.com/archive.mp4", "media.example.com", "duplicate"
            )

    def test_monitor_records_missing_credentials_without_crashing(self) -> None:
        channel = self.store.create_channel({
            "creator_id": "kai-cenat", "platform": "twitch", "platform_key": "kaicenat",
            "channel_url": "https://www.twitch.tv/kaicenat", "enabled": True,
        })
        settings = Settings(self.root, self.root / "rivet.db", self.root, 0, "token", 100, 1, False)
        result = PlatformMonitor(settings, self.store).poll_once()
        self.assertTrue(result["ok"])
        self.assertIn("TWITCH_CLIENT_ID", result["platforms"]["twitch"]["error"])
        self.assertIn("TWITCH_CLIENT_ID", self.store.get_channel(channel["id"])["last_error"])

    def test_monitor_rejects_overlapping_poll(self) -> None:
        settings = Settings(self.root, self.root / "rivet.db", self.root, 0, "token", 100, 1, False)
        monitor = PlatformMonitor(settings, self.store)
        monitor.poll_lock.acquire()
        try:
            self.assertEqual({"ok": False, "busy": True, "platforms": {}}, monitor.poll_once())
        finally:
            monitor.poll_lock.release()

    def test_channel_can_be_paused_and_resumed_with_audit_trail(self) -> None:
        channel = self.store.create_channel({
            "creator_id": "kai-cenat", "platform": "twitch", "platform_key": "kaicenat",
            "channel_url": "https://www.twitch.tv/kaicenat", "enabled": True,
        })
        self.assertEqual(0, self.store.set_channel_enabled(channel["id"], False)["enabled"])
        self.assertEqual([], self.store.list_channels(enabled_only=True))
        self.assertEqual(1, self.store.set_channel_enabled(channel["id"], True)["enabled"])
        event_types = {event["event_type"] for event in self.store.list_audit_events()}
        self.assertIn("channel.paused", event_types)
        self.assertIn("channel.enabled", event_types)

    def test_audit_events_are_readable_and_structured(self) -> None:
        events = self.store.list_audit_events()
        self.assertTrue(events)
        self.assertIsInstance(events[0]["details"], dict)

    def test_platform_safety_and_monetization_ledger(self) -> None:
        account = self.store.create_platform_account({
            "platform": "youtube", "handle": "Rivet Clips", "health_status": "healthy",
            "monetization_status": "not_applied", "monetization": {"subscribers": 0},
        })
        incident = self.store.create_safety_incident({
            "account_id": account["id"], "kind": "copyright_claim", "severity": "warning",
            "title": "Test claim", "occurred_at": 1000, "details": {"source": "test"},
        })
        self.assertEqual("not_applied", self.store.list_platform_accounts()[0]["monetization_status"])
        self.assertEqual("youtube", self.store.list_safety_incidents()[0]["platform"])
        self.assertEqual({"source": "test"}, self.store.get_safety_incident(incident["id"])["details"])


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

    def test_platform_channel_host_allowlist(self) -> None:
        from urllib.parse import urlparse
        allowed = {"twitch.tv", "www.twitch.tv"}
        self.assertIn(urlparse("https://www.twitch.tv/kaicenat").hostname, allowed)
        self.assertNotIn(urlparse("https://example.com/kaicenat").hostname, allowed)


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

    @patch("rivet.media.shutil.which", return_value=None)
    @patch("rivet.media.subprocess.run")
    def test_duration_falls_back_to_ffmpeg(self, run, _which) -> None:
        run.return_value.stderr = "Duration: 00:01:02.50, start: 0.0"
        self.assertEqual(62.5, probe_duration(Path("source.mp4")))

    def test_installable_app_assets_exist_and_protect_api_cache(self) -> None:
        package = Path(__file__).parents[1] / "rivet"
        manifest = json.loads((package / "manifest.webmanifest").read_text())
        self.assertEqual("standalone", manifest["display"])
        self.assertTrue((package / "icon.svg").is_file())
        self.assertIn('url.pathname.startsWith("/api/")', (package / "sw.js").read_text())


if __name__ == "__main__":
    unittest.main()

