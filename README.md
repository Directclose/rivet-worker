# Rivet worker

Rivet turns authorized long-form recordings into reviewable vertical clip candidates. This repository continues the deployed `v5-control-plane` prototype with a persistent SQLite ledger, a real FFmpeg processing worker, and fail-closed review and publishing gates.

## Verified scope

- A source cannot be queued unless it has an explicit rights note, ingestion authorization, an exact HTTPS URL, and a matching public host.
- The worker downloads only that exact registered URL and rejects redirects, private-network destinations, custom ports, credentials in URLs, oversized bodies, and unsupported content types.
- FFmpeg finds dense audio sections, renders up to three 1080x1920 H.264/AAC candidates, and burns supplied SRT captions.
- Every clip stops in `pending_review`. An authenticated reviewer must approve it with a note.
- Publishing additionally requires republishing rights, an enabled destination, an approval note, and the global `RIVET_PUBLISH_EXECUTION_ENABLED=true` switch.
- Publishing is not enabled by the included Render configuration. A `manual_export` destination creates a manifest for an owner upload; a `webhook` destination is an integration seam, not proof of platform publication.

The detector is signal-based. It identifies dense non-silent segments; it does not yet understand semantic quality or automatically transcribe speech.

## Run locally

Python 3.11+ is required. The worker also requires `ffmpeg` and `ffprobe`.

```bash
export RIVET_DATA_DIR="$PWD/data"
export RIVET_ADMIN_TOKEN="replace-with-a-long-random-value"
python -m rivet.service
```

Readiness is available at `GET /ready`. `GET /health` only confirms that the HTTP process is responding.

Run the dependency-free test suite:

```bash
python -m unittest discover -s tests -v
python -m compileall -q rivet
```

## Controlled API flow

All mutations require `Authorization: Bearer $RIVET_ADMIN_TOKEN`.

1. `POST /api/sources` with `name`, `source_url`, `rights_note`, `ingest_authorized`, and `republish_authorized`.
2. `POST /api/jobs` with the source ID and optional `captions_srt`.
3. Inspect `GET /api/jobs` and `GET /api/clips`.
4. `POST /api/clips/{clip_id}/review` with `decision` (`approved` or `rejected`) and a non-empty `note`.
5. Create an explicitly enabled `manual_export` or `webhook` destination with `POST /api/destinations`.
6. Stage an approved clip using `POST /api/publishing` with `clip_id`, `destination_id`, and `approval_note`.

No endpoint bypasses source rights, clip review, destination enablement, or the global publishing switch.

## Render

`render.yaml` defines one Docker web service so the API, queue worker, publisher, SQLite ledger, and artifacts share one attached disk. The service is deliberately single-instance because Render disks cannot be shared between instances.

Important: Render persistent disks are available only on paid services. Applying this Blueprint will therefore incur compute and disk charges. The file is configuration, not evidence of deployment. Confirm current pricing and approve the spend before applying it.

The Blueprint keeps publishing disabled and generates the admin token inside Render. After an authorized deploy, verify all of the following before calling it operational:

- the deploy reaches Render's `live` state;
- `/health` returns 200;
- `/ready` returns 200 with both FFmpeg tools present;
- the mounted path is `/var/lib/rivet`;
- a rights-authorized test source moves from queued to `pending_review` and produces playable 9:16 files;
- no publishing occurs until a reviewer approves the clip and the separate publishing switch is enabled.

## Recovery provenance

The previously deployed Vercel project `rivet` was inspected before this repository's first commit. Its latest production package contained `Dockerfile` and `rivet_live.py`; its API identified itself as `v5-control-plane`, reported no jobs or clips, kept source authorization false, and explicitly reported `worker: not_configured_on_vercel`. This repository retains those safety boundaries and adds the missing persistent execution path. The Vercel deployment itself was not changed.

