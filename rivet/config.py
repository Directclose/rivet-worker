from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    artifacts_dir: Path
    port: int
    admin_token: str
    max_source_bytes: int
    worker_poll_seconds: float
    publish_execution_enabled: bool
    storage_durability: str = "unknown"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("RIVET_DATA_DIR", "/var/lib/rivet")).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            db_path=Path(os.getenv("RIVET_DB_PATH", str(data_dir / "rivet.db"))).expanduser().resolve(),
            artifacts_dir=Path(os.getenv("RIVET_ARTIFACTS_DIR", str(data_dir / "artifacts"))).expanduser().resolve(),
            port=int(os.getenv("PORT", "8787")),
            admin_token=os.getenv("RIVET_ADMIN_TOKEN", ""),
            max_source_bytes=int(os.getenv("RIVET_MAX_SOURCE_BYTES", str(2 * 1024**3))),
            worker_poll_seconds=float(os.getenv("RIVET_WORKER_POLL_SECONDS", "2")),
            publish_execution_enabled=_bool("RIVET_PUBLISH_EXECUTION_ENABLED"),
            storage_durability=os.getenv("RIVET_STORAGE_DURABILITY", "unknown").strip().lower(),
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

