"""Reece Speech API — HTTP routes, startup, auth and limits."""

import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import audio
from .auth import extract_bearer, is_authorized
from .config import Settings, load_settings
from .jobs import RESTART_MESSAGE, JobItem, JobManager, transcribe_plan
from .ratelimit import RateLimiter
from .store import JobStore, MemoryStore, StoreError, build_store, now_iso
from .transcriber import Transcriber, is_valid_language

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("speech.api")

# Multipart framing adds a little on top of the file itself.
MULTIPART_OVERHEAD = 1024 * 1024
SOURCE_NAME_MAX = 200


def create_app(settings: Settings | None = None, transcriber=None, store: JobStore | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tr = transcriber
        if tr is None:
            tr = Transcriber(settings)
            tr.start_background_load()  # /health answers 503 until this finishes

        st = store or build_store(settings)
        try:
            # Jobs that were mid-flight when the last process died have lost their temp audio.
            marked = await run_in_threadpool(st.fail_unfinished, RESTART_MESSAGE)
            if marked:
                log.info("marked %d unfinished job(s) failed after restart", marked)
        except StoreError as exc:
            log.error("job store unreachable at startup (%s); falling back to memory", exc)
            st = MemoryStore()

        manager = JobManager(st, tr, settings.whisper_model, settings.job_workers)
        manager.start()
        app.state.transcriber, app.state.store, app.state.jobs = tr, st, manager
        yield
        manager.stop()

    app = FastAPI(title="Reece Speech API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    limiter = RateLimiter(settings.rate_limit_per_min)

    @app.middleware("http")
    async def gatekeeper(request: Request, call_next):
        """Auth, rate limit and size checks run BEFORE the body is read, so a caller without
        the key cannot make us spool a 1 GB upload to disk just to be told 401."""
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)
        authorization = request.headers.get("authorization")
        if not is_authorized(authorization, settings.speech_api_key):
            return JSONResponse({"detail": "Missing or invalid API key"}, status_code=401)
        if not limiter.allow(extract_bearer(authorization)):
            return JSONResponse({"detail": "Rate limit exceeded — slow down"}, status_code=429)
        tr = getattr(request.app.state, "transcriber", None)
        if tr is None or not tr.loaded:
            return JSONResponse({"detail": "Model is still loading — try again shortly"}, status_code=503)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_upload_bytes + MULTIPART_OVERHEAD:
            return JSONResponse(
                {"detail": f"Audio is larger than the {settings.max_upload_mb} MB limit"}, status_code=413
            )
        return await call_next(request)

    @app.get("/health")
    async def health(request: Request):
        tr = getattr(request.app.state, "transcriber", None)
        loaded = bool(tr and tr.loaded)
        body = {
            "status": "ok" if loaded else "loading",
            "model": settings.whisper_model,
            "compute_type": settings.whisper_compute_type,
            "model_loaded": loaded,
        }
        return JSONResponse(body, status_code=200 if loaded else 503)

    async def fetch_audio(workdir: str, upload: UploadFile | None, audio_url: str | None) -> tuple[str, str]:
        """Put exactly one of upload / audio_url on disk. Returns (path, source_name)."""
        has_upload = upload is not None and bool(upload.filename)
        has_url = bool(audio_url and audio_url.strip())
        if has_upload == has_url:
            raise HTTPException(400, "Send exactly one of: audio (file) or audio_url")
        path = os.path.join(workdir, "source")  # no extension: the format is judged by content
        if has_upload:
            await audio.save_upload(upload, path, settings.max_upload_bytes)
            name = upload.filename
        else:
            url = audio_url.strip()
            await run_in_threadpool(audio.download_url, url, path, settings.max_upload_bytes)
            # Path basename only — a query string can carry a signed-URL token.
            name = os.path.basename(urlparse(url).path) or "audio_url"
        return path, name[:SOURCE_NAME_MAX]

    def check_language(language: str) -> str:
        language = (language or "auto").strip().lower()
        if not is_valid_language(language):
            raise HTTPException(400, f"Unknown language code: {language!r}. Use a code like 'en' or 'auto'")
        return language

    @app.post("/v1/transcribe")
    async def transcribe(
        request: Request,
        audio_file: UploadFile | None = File(None, alias="audio"),
        audio_url: str | None = Form(None),
        language: str = Form("auto"),
        timestamps: bool = Form(True),
    ):
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        language = check_language(language)
        workdir = tempfile.mkdtemp(prefix="speech-sync-")
        duration = None
        status = 500
        too_long = f"Audio is longer than {settings.sync_max_seconds:g} seconds. Use POST /v1/jobs for long audio"
        try:
            src, _ = await fetch_audio(workdir, audio_file, audio_url)
            info = await run_in_threadpool(audio.probe, src)
            # Reject early from the header when we can, before spending CPU on decoding.
            if info.duration is not None and info.duration > settings.sync_max_seconds:
                raise HTTPException(413, too_long)
            wav = os.path.join(workdir, "normalized.wav")
            await run_in_threadpool(audio.normalize, src, wav)
            audio.remove_quietly(src)
            duration = await run_in_threadpool(audio.wav_duration, wav)
            if duration > settings.sync_max_seconds:
                raise HTTPException(413, too_long)
            plan = audio.plan_chunks(duration, settings.chunk_seconds)
            merged = await run_in_threadpool(
                transcribe_plan, request.app.state.transcriber, wav, plan, language, workdir
            )
            status = 200
            return {
                "text": merged["text"],
                "language": merged["language"],
                "duration": round(duration, 3),
                "model": settings.whisper_model,
                "segments": merged["segments"] if timestamps else [],
            }
        except audio.AudioError as exc:
            status = exc.status_code
            raise HTTPException(exc.status_code, exc.message) from exc
        except HTTPException as exc:
            status = exc.status_code
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)  # audio is deleted on success AND failure
            log.info("transcribe id=%s status=%d duration=%s seconds=%.1f model=%s",
                     request_id, status, f"{duration:.1f}" if duration is not None else "?",
                     time.monotonic() - started, settings.whisper_model)

    @app.post("/v1/jobs", status_code=202)
    async def create_job(
        request: Request,
        audio_file: UploadFile | None = File(None, alias="audio"),
        audio_url: str | None = Form(None),
        language: str = Form("auto"),
        timestamps: bool = Form(True),
    ):
        language = check_language(language)
        job_id = str(uuid.uuid4())
        workdir = tempfile.mkdtemp(prefix=f"speech-job-{job_id[:8]}-")
        handed_off = False
        try:
            src, source_name = await fetch_audio(workdir, audio_file, audio_url)
            info = await run_in_threadpool(audio.probe, src)
            duration, normalized = info.duration, False
            if duration is None:
                # Some containers (browser-recorded webm) carry no duration; decode now to
                # learn it, since the caller is promised chunks_total up front.
                wav = os.path.join(workdir, "normalized.wav")
                await run_in_threadpool(audio.normalize, src, wav)
                audio.remove_quietly(src)
                src, normalized = wav, True
                duration = await run_in_threadpool(audio.wav_duration, wav)
            plan = audio.plan_chunks(duration, settings.chunk_seconds)
            job = {
                "id": job_id, "status": "queued", "source_name": source_name,
                "duration_seconds": round(duration, 3), "model": settings.whisper_model,
                "chunks_total": len(plan), "chunks_done": 0,
                "created_at": now_iso(), "updated_at": now_iso(),
            }
            chunks = [{"chunk_index": c.index, "start_seconds": c.start, "end_seconds": c.end,
                       "status": "queued", "attempts": 0} for c in plan]
            try:
                await run_in_threadpool(request.app.state.store.create_job, job, chunks)
            except StoreError as exc:
                log.error("job id=%s could not be stored: %s", job_id, exc)
                raise HTTPException(503, "Job store unavailable — try again shortly") from exc
            request.app.state.jobs.enqueue(JobItem(
                job_id=job_id, workdir=workdir, source_path=src, normalized=normalized,
                plan=plan, language=language, timestamps=timestamps,
            ))
            handed_off = True  # the worker now owns the temp dir and deletes it when done
            log.info("job id=%s status=queued duration=%.1f chunks=%d model=%s",
                     job_id, duration, len(plan), settings.whisper_model)
            return {"job_id": job_id, "status": "queued", "chunks_total": len(plan)}
        except audio.AudioError as exc:
            raise HTTPException(exc.status_code, exc.message) from exc
        finally:
            if not handed_off:
                shutil.rmtree(workdir, ignore_errors=True)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        try:
            uuid.UUID(job_id)
        except ValueError:
            raise HTTPException(404, "Job not found")
        try:
            job = await run_in_threadpool(request.app.state.store.get_job, job_id)
        except StoreError as exc:
            log.error("job id=%s could not be read: %s", job_id, exc)
            raise HTTPException(503, "Job store unavailable — try again shortly") from exc
        if job is None:
            raise HTTPException(404, "Job not found")
        return job_view(job)

    return app


def job_view(job: dict) -> dict:
    """The public shape of a job (the API contract), from the stored row."""
    result = None
    if job["status"] == "done":
        result = {
            "text": job.get("text") or "",
            "language": job.get("language"),
            "duration": float(job["duration_seconds"]) if job.get("duration_seconds") is not None else None,
            "model": job.get("model"),
            "segments": job.get("segments") or [],
        }
    return {
        "job_id": job["id"],
        "status": job["status"],
        "chunks_total": job.get("chunks_total"),
        "chunks_done": job.get("chunks_done", 0),
        "chunks": [{"index": c["chunk_index"], "status": c["status"]}
                   for c in sorted(job.get("chunks") or [], key=lambda c: c["chunk_index"])],
        "result": result,
        "error": job.get("error"),
    }


app = create_app()
