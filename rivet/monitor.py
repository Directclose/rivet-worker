from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from typing import Any

from .config import Settings
from .db import Store


def _json_request(url: str, *, headers: dict[str, str] | None = None,
                  data: bytes | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


class PlatformMonitor:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.stopped = threading.Event()
        self.poll_lock = threading.Lock()

    def stop(self) -> None:
        self.stopped.set()

    def run(self) -> None:
        while not self.stopped.is_set():
            self.poll_once()
            self.stopped.wait(self.settings.monitor_poll_seconds)

    def poll_once(self) -> dict[str, Any]:
        if not self.poll_lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "platforms": {}}
        results: dict[str, Any] = {}
        try:
            channels = self.store.list_channels(enabled_only=True)
            for platform in ("twitch", "youtube", "kick"):
                selected = [item for item in channels if item["platform"] == platform]
                if not selected:
                    continue
                try:
                    observations = getattr(self, f"_poll_{platform}")(selected)
                    seen: set[str] = set()
                    for channel_id, observation in observations:
                        seen.add(observation["external_id"])
                        self.store.upsert_candidate(channel_id, observation)
                    self.store.close_unseen_live_candidates(platform, seen)
                    for channel in selected:
                        self.store.mark_channel_checked(channel["id"])
                    results[platform] = {"channels": len(selected), "observations": len(observations), "error": None}
                except Exception as exc:
                    message = str(exc)[:500]
                    for channel in selected:
                        self.store.mark_channel_checked(channel["id"], message)
                    results[platform] = {"channels": len(selected), "observations": 0, "error": message}
            return {"ok": True, "busy": False, "platforms": results}
        finally:
            self.poll_lock.release()

    def _poll_twitch(self, channels: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        if not self.settings.twitch_client_id or not self.settings.twitch_client_secret:
            raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")
        body = urllib.parse.urlencode({
            "client_id": self.settings.twitch_client_id,
            "client_secret": self.settings.twitch_client_secret,
            "grant_type": "client_credentials",
        }).encode()
        token = _json_request("https://id.twitch.tv/oauth2/token", data=body)["access_token"]
        headers = {"Client-Id": self.settings.twitch_client_id, "Authorization": f"Bearer {token}"}
        logins = [c["platform_key"] for c in channels]
        users_url = "https://api.twitch.tv/helix/users?" + urllib.parse.urlencode({"login": logins}, doseq=True)
        users = _json_request(users_url, headers=headers).get("data", [])
        channel_by_login = {c["platform_key"].lower(): c for c in channels}
        channel_by_user = {u["id"]: channel_by_login[u["login"].lower()] for u in users if u["login"].lower() in channel_by_login}
        if not channel_by_user:
            return []
        stream_url = "https://api.twitch.tv/helix/streams?" + urllib.parse.urlencode(
            {"user_id": list(channel_by_user)}, doseq=True
        )
        streams = _json_request(stream_url, headers=headers).get("data", [])
        return [(channel_by_user[s["user_id"]]["id"], {
            "platform": "twitch", "external_id": s["id"], "title": s.get("title", ""),
            "page_url": channel_by_user[s["user_id"]]["channel_url"], "state": "live",
            "started_at": s.get("started_at"), "viewer_count": s.get("viewer_count"), "raw": s,
        }) for s in streams]

    def _poll_youtube(self, channels: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        if not self.settings.youtube_api_key:
            raise RuntimeError("YOUTUBE_API_KEY is required")
        results: list[tuple[str, dict[str, Any]]] = []
        for channel in channels:
            query = urllib.parse.urlencode({
                "part": "snippet", "channelId": channel["platform_key"], "eventType": "live",
                "type": "video", "maxResults": 10, "key": self.settings.youtube_api_key,
            })
            data = _json_request(f"https://www.googleapis.com/youtube/v3/search?{query}")
            for item in data.get("items", []):
                video_id = item["id"]["videoId"]
                results.append((channel["id"], {
                    "platform": "youtube", "external_id": video_id,
                    "title": item["snippet"].get("title", ""),
                    "page_url": f"https://www.youtube.com/watch?v={video_id}", "state": "live",
                    "started_at": item["snippet"].get("publishedAt"), "raw": item,
                }))
        return results

    def _poll_kick(self, channels: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        if not self.settings.kick_client_id or not self.settings.kick_client_secret:
            raise RuntimeError("KICK_CLIENT_ID and KICK_CLIENT_SECRET are required")
        body = urllib.parse.urlencode({
            "client_id": self.settings.kick_client_id, "client_secret": self.settings.kick_client_secret,
            "grant_type": "client_credentials",
        }).encode()
        token = _json_request("https://id.kick.com/oauth/token", data=body)["access_token"]
        query = urllib.parse.urlencode({"slug": [c["platform_key"] for c in channels]}, doseq=True)
        data = _json_request(
            f"https://api.kick.com/public/v1/channels?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        channel_by_slug = {c["platform_key"].lower(): c for c in channels}
        results = []
        for item in data.get("data", []):
            stream = item.get("stream") or {}
            if not stream.get("is_live"):
                continue
            channel = channel_by_slug.get(item.get("slug", "").lower())
            if not channel:
                continue
            results.append((channel["id"], {
                "platform": "kick", "external_id": str(stream.get("key") or f"{item['slug']}:{stream.get('start_time')}"),
                "title": item.get("stream_title", ""), "page_url": channel["channel_url"], "state": "live",
                "started_at": stream.get("start_time"), "viewer_count": stream.get("viewer_count"), "raw": item,
            }))
        return results
