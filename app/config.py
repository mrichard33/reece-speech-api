"""All settings come from environment variables, with the defaults from .env.example."""

import logging
import os
from dataclasses import dataclass, replace
from typing import Mapping

log = logging.getLogger("speech.config")


class ConfigError(RuntimeError):
    """Raised when the service cannot start with the given environment."""


@dataclass(frozen=True)
class Settings:
    speech_api_key: str
    whisper_model: str = "small"
    whisper_compute_type: str = "int8"
    whisper_device: str = "cpu"
    whisper_cpu_threads: int = 0
    model_cache_dir: str = "/models"
    max_upload_mb: int = 1024
    sync_max_seconds: float = 900.0
    chunk_seconds: float = 1800.0
    job_workers: int = 1
    rate_limit_per_min: int = 30
    job_store: str = "memory"
    supabase_url: str = ""
    supabase_service_key: str = ""

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def with_overrides(self, **changes) -> "Settings":
        return replace(self, **changes)


def _get(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, "")
    return value.strip() if value and value.strip() else default


def _number(env: Mapping[str, str], name: str, default, cast, minimum):
    raw = _get(env, name, str(default))
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    key = env.get("SPEECH_API_KEY", "").strip()
    if not key:
        # Refuse to start: an unauthenticated transcription endpoint is an open CPU for anyone.
        raise ConfigError("SPEECH_API_KEY is required — the service will not start without it")

    job_store = _get(env, "JOB_STORE", "memory").lower()
    supabase_url = _get(env, "SUPABASE_URL", "").rstrip("/")
    supabase_key = _get(env, "SUPABASE_SERVICE_KEY", "")
    if job_store not in ("memory", "supabase"):
        log.warning("JOB_STORE=%r is not recognised; using memory", job_store)
        job_store = "memory"
    if job_store == "supabase" and not (supabase_url and supabase_key):
        log.warning("JOB_STORE=supabase but SUPABASE_URL/SUPABASE_SERVICE_KEY are missing; using memory")
        job_store = "memory"

    return Settings(
        speech_api_key=key,
        whisper_model=_get(env, "WHISPER_MODEL", "small"),
        whisper_compute_type=_get(env, "WHISPER_COMPUTE_TYPE", "int8"),
        whisper_device=_get(env, "WHISPER_DEVICE", "cpu"),
        whisper_cpu_threads=_number(env, "WHISPER_CPU_THREADS", 0, int, 0),
        model_cache_dir=_get(env, "MODEL_CACHE_DIR", "/models"),
        max_upload_mb=_number(env, "MAX_UPLOAD_MB", 1024, int, 1),
        sync_max_seconds=_number(env, "SYNC_MAX_SECONDS", 900, float, 1),
        chunk_seconds=_number(env, "CHUNK_SECONDS", 1800, float, 1),
        job_workers=_number(env, "JOB_WORKERS", 1, int, 1),
        rate_limit_per_min=_number(env, "RATE_LIMIT_PER_MIN", 30, int, 1),
        job_store=job_store,
        supabase_url=supabase_url,
        supabase_service_key=supabase_key,
    )
