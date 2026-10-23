from __future__ import annotations

import hmac
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .authorization import AuthorizationError, authorize_source
from .config import Settings
from .db import Store
from .media import check_runtime, validate_srt


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "Rivet/0.6"

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        print(json.dumps({"event": "http", "message": fmt % args}))

    def _json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, body: str) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _media(self, clip_id: str) -> None:
        if not self._authenticated():
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_auth_required"})
            return
        clip = self.store.get_clip(clip_id)
        path = Path(clip["output_path"]).resolve()
        root = self.settings.artifacts_dir.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise FileNotFoundError("clip artifact is unavailable")
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                self.wfile.write(chunk)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("request body must be between 1 byte and 1 MB")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _authenticated(self) -> bool:
        expected = self.settings.admin_token
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")

    def _require_auth(self) -> bool:
        if self._authenticated():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "admin_auth_required"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/":
                dashboard = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
                self._html(200, dashboard)
            elif path == "/health":
                self._json(200, {"ok": True, "service": "rivet", "version": __version__})
            elif path == "/ready":
                runtime = check_runtime()
                ready = runtime["ffmpeg"] and runtime["probe_available"] and self.settings.db_path.parent.exists()
                self._json(
                    200 if ready else 503,
                    {
                        "ok": ready,
                        "runtime": runtime,
                        "data_path": str(self.settings.data_dir),
                        "storage_durability": self.settings.storage_durability,
                        "durable_storage": self.settings.storage_durability == "persistent",
                    },
                )
            elif path == "/api/state":
                self._json(200, {
                    "ok": True, "service": "rivet", "version": __version__, **self.store.counts(),
                    "publish_execution_enabled": self.settings.publish_execution_enabled,
                    "platform_credentials": {
                        "twitch": bool(self.settings.twitch_client_id and self.settings.twitch_client_secret),
                        "youtube": bool(self.settings.youtube_api_key),
                        "kick": bool(self.settings.kick_client_id and self.settings.kick_client_secret),
                    },
                })
            elif path == "/api/sources":
                if self._require_auth():
                    self._json(200, {"sources": self.store.list_sources()})
            elif path == "/api/creators":
                if self._require_auth():
                    self._json(200, {"creators": self.store.list_creators()})
            elif path == "/api/channels":
                if self._require_auth():
                    self._json(200, {"channels": self.store.list_channels()})
            elif path == "/api/candidates":
                if self._require_auth():
                    self._json(200, {"candidates": self.store.list_candidates()})
            elif path == "/api/audit":
                if self._require_auth():
                    self._json(200, {"events": self.store.list_audit_events()})
            elif path == "/api/jobs":
                if self._require_auth():
                    self._json(200, {"jobs": self.store.list_jobs()})
            elif path == "/api/clips":
                if self._require_auth():
                    self._json(200, {"clips": self.store.list_clips()})
            elif path == "/api/destinations":
                if self._require_auth():
                    self._json(200, {"destinations": self.store.list_destinations()})
            elif path == "/api/publishing":
                if self._require_auth():
                    self._json(200, {"publishing": self.store.list_publishes()})
            elif match := re.fullmatch(r"/api/clips/([^/]+)/media", path):
                self._media(match.group(1))
            else:
                self._json(404, {"ok": False, "error": "not_found", "path": path})
        except Exception as exc:
            self._json(500, {"ok": False, "error": "internal_error", "detail": str(exc)[:500]})

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            data = self._body()
            if path == "/api/sources":
                source_url = str(data.get("source_url", ""))
                parsed = urlparse(source_url)
                if parsed.scheme != "https" or not parsed.hostname:
                    raise ValueError("source_url must be an HTTPS URL")
                if not str(data.get("name", "")).strip():
                    raise ValueError("source name is required")
                if not str(data.get("rights_note", "")).strip():
                    raise ValueError("rights_note is required")
                source = self.store.create_source({
                    "id": data.get("id"), "name": str(data.get("name", "")).strip(),
                    "source_url": source_url, "allowed_host": parsed.hostname.lower(),
                    "ingest_authorized": data.get("ingest_authorized") is True,
                    "republish_authorized": data.get("republish_authorized") is True,
                    "rights_note": str(data["rights_note"]).strip(),
                })
                self._json(201, source)
            elif path == "/api/channels":
                platform = str(data.get("platform", "")).lower()
                if platform not in {"twitch", "youtube", "kick"}:
                    raise ValueError("platform must be twitch, youtube, or kick")
                platform_key = str(data.get("platform_key", "")).strip()
                channel_url = str(data.get("channel_url", "")).strip()
                parsed = urlparse(channel_url)
                if not platform_key or parsed.scheme != "https" or not parsed.hostname:
                    raise ValueError("platform_key and an HTTPS channel_url are required")
                allowed_hosts = {
                    "twitch": {"twitch.tv", "www.twitch.tv"},
                    "youtube": {"youtube.com", "www.youtube.com"},
                    "kick": {"kick.com", "www.kick.com"},
                }
                if parsed.hostname.lower() not in allowed_hosts[platform]:
                    raise ValueError(f"channel_url must be an official {platform} URL")
                if str(data.get("creator_id", "")) not in {c["id"] for c in self.store.list_creators()}:
                    raise KeyError(str(data.get("creator_id", "")))
                self._json(201, self.store.create_channel({
                    "id": data.get("id"), "creator_id": str(data["creator_id"]), "platform": platform,
                    "platform_key": platform_key, "channel_url": channel_url, "enabled": data.get("enabled") is not False,
                }))
            elif path == "/api/jobs":
                source = self.store.get_source(str(data.get("source_id", "")))
                source_url = str(data.get("source_url") or source["source_url"])
                authorize_source(source, source_url)
                captions = data.get("captions_srt")
                if captions is not None:
                    validate_srt(str(captions))
                self._json(201, self.store.create_job(source["id"], source_url, captions))
            elif path == "/api/destinations":
                kind = str(data.get("kind", ""))
                if kind not in {"manual_export", "webhook"}:
                    raise ValueError("destination kind must be manual_export or webhook")
                self._json(201, self.store.create_destination({
                    "id": data.get("id"), "kind": kind,
                    "display_name": str(data.get("display_name", "")).strip(),
                    "enabled": data.get("enabled") is True, "config": data.get("config") or {},
                }))
            elif match := re.fullmatch(r"/api/clips/([^/]+)/review", path):
                note = str(data.get("note", "")).strip()
                if not note:
                    raise ValueError("review note is required")
                self._json(200, self.store.review_clip(match.group(1), str(data.get("decision")), note))
            elif path == "/api/publishing":
                approval_note = str(data.get("approval_note", "")).strip()
                if not approval_note:
                    raise ValueError("approval_note is required")
                self._json(201, self.store.queue_publish(
                    str(data.get("clip_id", "")), str(data.get("destination_id", "")), approval_note
                ))
            else:
                self._json(404, {"ok": False, "error": "not_found", "path": path})
        except KeyError as exc:
            self._json(404, {"ok": False, "error": "missing_resource", "detail": str(exc)})
        except (ValueError, AuthorizationError, PermissionError) as exc:
            self._json(400, {"ok": False, "error": "request_rejected", "detail": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": "internal_error", "detail": str(exc)[:500]})


def build_server(settings: Settings, store: Store) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), ApiHandler)
    server.settings, server.store = settings, store  # type: ignore[attr-defined]
    return server


def main() -> None:
    settings = Settings.from_env()
    settings.prepare()
    build_server(settings, Store(settings.db_path)).serve_forever()


if __name__ == "__main__":
    main()

