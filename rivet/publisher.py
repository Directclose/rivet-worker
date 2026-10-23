from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from .config import Settings
from .db import Store


def dispatch(store: Store, settings: Settings, item: dict) -> None:
    if not settings.publish_execution_enabled:
        raise PermissionError("publishing execution is disabled globally")
    clip = store.get_clip(item["clip_id"])
    destination = store.get_destination(item["destination_id"])
    if clip["status"] != "approved" or not destination["enabled"]:
        raise PermissionError("approval or destination authorization changed")
    if destination["kind"] == "manual_export":
        manifest_path = Path(clip["output_path"]).with_suffix(".publish.json")
        manifest_path.write_text(json.dumps({
            "clip_id": clip["id"], "media_path": clip["output_path"],
            "destination": destination["display_name"], "approval_note": item["approval_note"],
        }, indent=2), encoding="utf-8")
        store.set_publish_status(item["id"], "ready_for_manual_upload", external_id=str(manifest_path))
        return
    if destination["kind"] == "webhook":
        url = str(destination["config"].get("url", ""))
        if not url.startswith("https://"):
            raise ValueError("webhook destination requires an HTTPS URL")
        payload = json.dumps({"clip_id": clip["id"], "media_path": clip["output_path"], "approved": True}).encode()
        request = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"publisher webhook returned {response.status}")
            external_id = response.headers.get("X-External-Id")
        store.set_publish_status(item["id"], "dispatched", external_id=external_id)
        return
    raise ValueError("unsupported destination kind")


def main() -> None:
    settings = Settings.from_env()
    settings.prepare()
    if not settings.publish_execution_enabled:
        raise SystemExit("publishing execution is disabled; no outbox items were claimed")
    store = Store(settings.db_path)
    while True:
        item = store.claim_publish()
        if not item:
            time.sleep(2)
            continue
        try:
            dispatch(store, settings, item)
        except Exception as exc:
            store.set_publish_status(item["id"], "failed", error=str(exc)[:2000])


if __name__ == "__main__":
    main()
