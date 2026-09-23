"""Where job status lives. Memory by default; Supabase when JOB_STORE=supabase.

A job is a dict whose keys match the `speech_jobs` columns, plus "chunks": a list of dicts
whose keys match `speech_job_chunks`. Only status and text are stored — never audio.
"""

import copy
import logging
import threading
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("speech.store")

UNFINISHED = ("queued", "processing")
FINISHED = ("done", "failed")

# Finished jobs are dropped from memory after this long, so a long-running instance does
# not slowly fill its RAM with old transcripts.
MEMORY_JOB_TTL_SECONDS = 24 * 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreError(RuntimeError):
    pass


class JobStore:
    def create_job(self, job: dict, chunks: list[dict]) -> None:
        raise NotImplementedError

    def get_job(self, job_id: str) -> dict | None:
        """The job dict with a "chunks" list, or None if unknown."""
        raise NotImplementedError

    def update_job(self, job_id: str, **fields) -> None:
        raise NotImplementedError

    def update_chunk(self, job_id: str, chunk_index: int, **fields) -> None:
        raise NotImplementedError

    def fail_unfinished(self, message: str) -> int:
        """Mark every queued/processing job failed. Returns how many were marked."""
        raise NotImplementedError


class MemoryStore(JobStore):
    def __init__(self, ttl_seconds: float = MEMORY_JOB_TTL_SECONDS, clock=time.monotonic):
        self._jobs: dict[str, dict] = {}
        self._finished_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._clock = clock

    def _evict_expired(self) -> None:
        cutoff = self._clock() - self._ttl
        for job_id in [j for j, t in self._finished_at.items() if t < cutoff]:
            self._jobs.pop(job_id, None)
            self._finished_at.pop(job_id, None)

    def create_job(self, job: dict, chunks: list[dict]) -> None:
        with self._lock:
            self._evict_expired()
            self._jobs[job["id"]] = {**copy.deepcopy(job), "chunks": copy.deepcopy(chunks)}

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def update_job(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields, updated_at=now_iso())
            if job.get("status") in FINISHED:
                self._finished_at.setdefault(job_id, self._clock())

    def update_chunk(self, job_id: str, chunk_index: int, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for chunk in job["chunks"]:
                if chunk["chunk_index"] == chunk_index:
                    chunk.update(fields)

    def fail_unfinished(self, message: str) -> int:
        with self._lock:
            count = 0
            for job_id, job in self._jobs.items():
                if job["status"] in UNFINISHED:
                    job.update(status="failed", error=message, updated_at=now_iso(), completed_at=now_iso())
                    self._finished_at.setdefault(job_id, self._clock())
                    count += 1
            return count


class SupabaseStore(JobStore):
    """Talks to Supabase's REST API directly (PostgREST) — no SDK needed for four calls."""

    def __init__(self, url: str, service_key: str, client: httpx.Client | None = None):
        self.base = f"{url.rstrip('/')}/rest/v1"
        self.client = client or httpx.Client(timeout=15.0)
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, table: str, *, params=None, json=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            resp = self.client.request(method, f"{self.base}/{table}", params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise StoreError(f"Supabase {method} {table} failed: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise StoreError(f"Supabase {method} {table} returned HTTP {resp.status_code}")
        return resp

    def create_job(self, job: dict, chunks: list[dict]) -> None:
        self._request("POST", "speech_jobs", json=job, prefer="return=minimal")
        self._request("POST", "speech_job_chunks", json=[{**c, "job_id": job["id"]} for c in chunks],
                      prefer="return=minimal")

    def get_job(self, job_id: str) -> dict | None:
        rows = self._request("GET", "speech_jobs", params={"id": f"eq.{job_id}", "select": "*"}).json()
        if not rows:
            return None
        chunks = self._request("GET", "speech_job_chunks", params={
            "job_id": f"eq.{job_id}",
            "select": "chunk_index,start_seconds,end_seconds,status,attempts,error",
            "order": "chunk_index.asc",
        }).json()
        return {**rows[0], "chunks": chunks}

    def update_job(self, job_id: str, **fields) -> None:
        self._request("PATCH", "speech_jobs", params={"id": f"eq.{job_id}"},
                      json={**fields, "updated_at": now_iso()}, prefer="return=minimal")

    def update_chunk(self, job_id: str, chunk_index: int, **fields) -> None:
        self._request("PATCH", "speech_job_chunks",
                      params={"job_id": f"eq.{job_id}", "chunk_index": f"eq.{chunk_index}"},
                      json=fields, prefer="return=minimal")

    def fail_unfinished(self, message: str) -> int:
        resp = self._request(
            "PATCH", "speech_jobs",
            params={"status": "in.(queued,processing)", "select": "id"},
            json={"status": "failed", "error": message, "updated_at": now_iso(), "completed_at": now_iso()},
            prefer="return=representation",
        )
        return len(resp.json())


def build_store(settings) -> JobStore:
    if settings.job_store == "supabase":
        return SupabaseStore(settings.supabase_url, settings.supabase_service_key)
    return MemoryStore()
