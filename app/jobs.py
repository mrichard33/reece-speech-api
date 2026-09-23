"""Background job queue: normalise -> transcribe chunk by chunk (with retries) -> merge."""

import logging
import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass

from . import audio
from .audio import Chunk
from .store import JobStore, now_iso

log = logging.getLogger("speech.jobs")

MAX_ATTEMPTS = 3  # first try + 2 retries
RESTART_MESSAGE = "Service restarted — please resubmit"
ERROR_MAX_CHARS = 500


class ChunkFailed(Exception):
    def __init__(self, index: int, cause: Exception):
        super().__init__(f"chunk {index} failed after {MAX_ATTEMPTS} attempts: {type(cause).__name__}: {cause}")
        self.index = index


def merge_chunks(results: list[tuple[float, dict]]) -> dict:
    """Stitch per-chunk results into one transcript.

    `results` is [(chunk_start_seconds, {"text", "language", "segments"}), ...] in order.
    Each segment is shifted by its chunk's start so timestamps are relative to the whole file.
    """
    segments, texts, language = [], [], None
    for offset, result in results:
        for seg in result.get("segments") or []:
            segments.append({
                "start": round(seg["start"] + offset, 3),
                "end": round(seg["end"] + offset, 3),
                "text": seg["text"],
            })
        text = (result.get("text") or "").strip()
        if text:
            texts.append(text)
            language = language or result.get("language")
    if language is None and results:
        language = results[0][1].get("language")
    return {"text": " ".join(texts), "language": language, "segments": segments}


def transcribe_plan(transcriber, wav: str, plan: list[Chunk], language: str, workdir: str) -> dict:
    """Used by the sync route: same chunking and merge as jobs, no retries or status tracking."""
    results = []
    for chunk in plan:
        path = wav
        if len(plan) > 1:
            path = os.path.join(workdir, f"chunk_{chunk.index}.wav")
            audio.extract_chunk(wav, chunk, chunk.index == len(plan) - 1, path)
        try:
            results.append((chunk.start, transcriber.transcribe(path, language)))
        finally:
            if path != wav:
                audio.remove_quietly(path)
    return merge_chunks(results)


@dataclass
class JobItem:
    job_id: str
    workdir: str
    source_path: str          # raw upload, or already-normalised WAV when normalized=True
    normalized: bool
    plan: list[Chunk]
    language: str
    timestamps: bool


class JobManager:
    def __init__(self, store: JobStore, transcriber, model_name: str, workers: int = 1):
        self.store = store
        self.transcriber = transcriber
        self.model_name = model_name
        self.workers = workers
        self.queue: "queue.Queue[JobItem | None]" = queue.Queue()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"job-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 1.0) -> None:
        for _ in self._threads:
            self.queue.put(None)
        for t in self._threads:
            t.join(timeout)

    def enqueue(self, item: JobItem) -> None:
        self.queue.put(item)

    def _worker(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            try:
                self.process(item)
            except Exception:  # noqa: BLE001 - a worker thread must never die
                log.exception("job %s crashed outside the normal error path", item.job_id)

    def process(self, item: JobItem) -> None:
        started = time.monotonic()
        duration = None
        outcome = "failed"
        try:
            self.store.update_job(item.job_id, status="processing")
            if item.normalized:
                wav = item.source_path
            else:
                wav = os.path.join(item.workdir, "normalized.wav")
                audio.normalize(item.source_path, wav)
                audio.remove_quietly(item.source_path)  # free the disk as early as possible
            duration = audio.wav_duration(wav)

            results = []
            for chunk in item.plan:
                result = self._run_chunk(item, wav, chunk)
                results.append((chunk.start, result))
                self.store.update_job(item.job_id, chunks_done=len(results))

            merged = merge_chunks(results)
            self.store.update_job(
                item.job_id,
                status="done",
                text=merged["text"],
                language=merged["language"],
                segments=merged["segments"] if item.timestamps else [],
                duration_seconds=round(duration, 3),
                completed_at=now_iso(),
            )
            outcome = "done"
        except ChunkFailed as exc:
            self._fail(item.job_id, str(exc))
        except audio.AudioError as exc:
            self._fail(item.job_id, exc.message)
        except Exception as exc:  # noqa: BLE001
            self._fail(item.job_id, f"processing failed: {type(exc).__name__}: {exc}")
        finally:
            # Audio is deleted after success AND failure.
            shutil.rmtree(item.workdir, ignore_errors=True)
            # Never log transcript text or audio.
            log.info("job id=%s status=%s duration=%s chunks=%d seconds=%.1f model=%s",
                     item.job_id, outcome, f"{duration:.1f}" if duration is not None else "?",
                     len(item.plan), time.monotonic() - started, self.model_name)

    def _fail(self, job_id: str, message: str) -> None:
        try:
            self.store.update_job(job_id, status="failed", error=message[:ERROR_MAX_CHARS], completed_at=now_iso())
        except Exception:  # noqa: BLE001
            log.exception("job %s: could not record failure", job_id)

    def _run_chunk(self, item: JobItem, wav: str, chunk: Chunk) -> dict:
        """Transcribe one chunk, retrying up to MAX_ATTEMPTS. Finished chunks are never re-run."""
        single = len(item.plan) == 1
        path = wav if single else os.path.join(item.workdir, f"chunk_{chunk.index}.wav")
        last_error: Exception | None = None
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                self.store.update_chunk(item.job_id, chunk.index, status="processing", attempts=attempt)
                try:
                    if not single and not os.path.exists(path):
                        audio.extract_chunk(wav, chunk, chunk.index == len(item.plan) - 1, path)
                    result = self.transcriber.transcribe(path, item.language)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    log.warning("job id=%s chunk=%d attempt=%d failed: %s",
                                item.job_id, chunk.index, attempt, type(exc).__name__)
                    continue
                self.store.update_chunk(
                    item.job_id, chunk.index, status="done", error=None,
                    text=result["text"], segments=result["segments"] if item.timestamps else [],
                )
                return result
            self.store.update_chunk(item.job_id, chunk.index, status="failed",
                                    error=f"{type(last_error).__name__}: {last_error}"[:ERROR_MAX_CHARS])
            raise ChunkFailed(chunk.index, last_error)
        finally:
            if not single:
                audio.remove_quietly(path)
