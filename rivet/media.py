from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    start: float
    end: float
    score: float


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise MediaError("source redirected; register the final HTTPS URL explicitly")


def download(url: str, destination: Path, max_bytes: int) -> None:
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Rivet/0.6"})
    try:
        with opener.open(request, timeout=30) as response, destination.open("wb") as handle:
            content_type = response.headers.get_content_type()
            if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
                raise MediaError(f"source returned unsupported content type: {content_type}")
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise MediaError("source exceeds configured download limit")
                handle.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True, check=True, timeout=60 * 60)
    except FileNotFoundError as exc:
        raise MediaError(f"required executable is missing: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaError((exc.stderr or exc.stdout or "media command failed")[-2000:]) from exc


def probe_duration(path: Path) -> float:
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)
    ])
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if duration <= 0:
        raise MediaError("source duration is invalid")
    return duration


def detect_highlights(path: Path, duration: float, limit: int = 3) -> list[Candidate]:
    """Find dense spoken/music sections by excluding long silences.

    This is deterministic signal analysis, not a semantic claim about the content.
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-34dB:d=0.8", "-f", "null", "-"],
        text=True, capture_output=True, timeout=60 * 60,
    )
    if result.returncode not in (0, 1):
        raise MediaError(result.stderr[-2000:])
    silence_starts = [float(item) for item in re.findall(r"silence_start: ([0-9.]+)", result.stderr)]
    silence_ends = [float(item) for item in re.findall(r"silence_end: ([0-9.]+)", result.stderr)]
    boundaries = [0.0]
    for start, end in zip(silence_starts, silence_ends):
        boundaries.extend([start, end])
    boundaries.append(duration)
    active: list[tuple[float, float]] = []
    for index in range(0, len(boundaries) - 1, 2):
        start, end = boundaries[index], boundaries[index + 1]
        if end - start >= 8:
            active.append((start, end))
    if not active:
        active = [(0.0, duration)]
    candidates: list[Candidate] = []
    for start, end in active:
        cursor = start
        while cursor < end:
            clip_end = min(cursor + 45.0, end)
            if clip_end - cursor >= 12.0:
                length = clip_end - cursor
                density_score = min(length / 45.0, 1.0)
                center_bonus = 1.0 - abs(((cursor + clip_end) / 2 / duration) - 0.5) * 0.2
                candidates.append(Candidate(round(cursor, 3), round(clip_end, 3), round(density_score * center_bonus, 4)))
            cursor += 45.0
    return sorted(candidates, key=lambda item: (-item.score, item.start))[:limit]


def validate_srt(text: str) -> None:
    if len(text.encode("utf-8")) > 512_000:
        raise MediaError("captions exceed 512 KB")
    if "-->" not in text:
        raise MediaError("captions must be SRT text")


def render_vertical(source: Path, destination: Path, candidate: Candidate, captions: Path | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    video_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1"
    )
    if captions:
        escaped = str(captions).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        video_filter += f",subtitles='{escaped}':force_style='FontSize=18,Outline=2,Alignment=2,MarginV=110'"
    _run([
        "ffmpeg", "-y", "-hide_banner", "-ss", str(candidate.start), "-to", str(candidate.end),
        "-i", str(source), "-vf", video_filter, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(destination),
    ])
    if not destination.exists() or destination.stat().st_size == 0:
        raise MediaError("ffmpeg did not create a clip")


def check_runtime() -> dict[str, bool]:
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}

