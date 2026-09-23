"""The Whisper model. Loaded ONCE per process, used by both sync requests and job chunks."""

import logging
import os
import threading
import time

log = logging.getLogger("speech.transcriber")

try:
    from faster_whisper.tokenizer import _LANGUAGE_CODES as WHISPER_LANGUAGES
except ImportError:  # pragma: no cover - only if faster-whisper renames it
    WHISPER_LANGUAGES = ()

# One transcription at a time across the whole process. Two at once on a 2 vCPU box are
# each slower than running them back to back, and double the RAM.
MODEL_LOCK = threading.Lock()


def is_valid_language(language: str) -> bool:
    return language == "auto" or not WHISPER_LANGUAGES or language in WHISPER_LANGUAGES


class Transcriber:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.load_error: str | None = None
        self._loaded = threading.Event()

    @property
    def loaded(self) -> bool:
        return self._loaded.is_set()

    def load(self) -> None:
        from faster_whisper import WhisperModel

        s = self.settings
        os.makedirs(s.model_cache_dir, exist_ok=True)
        started = time.monotonic()
        # download_root on the Railway volume: first boot downloads, every redeploy reuses it.
        self.model = WhisperModel(
            s.whisper_model,
            device=s.whisper_device,
            compute_type=s.whisper_compute_type,
            cpu_threads=s.whisper_cpu_threads,
            download_root=s.model_cache_dir,
        )
        self._loaded.set()
        log.info("model loaded model=%s compute_type=%s seconds=%.1f",
                 s.whisper_model, s.whisper_compute_type, time.monotonic() - started)

    def start_background_load(self) -> threading.Thread:
        def run():
            try:
                self.load()
            except Exception as exc:  # noqa: BLE001
                self.load_error = f"{type(exc).__name__}: {exc}"
                log.exception("model failed to load; exiting so Railway restarts the service")
                # /health would stay 503 forever; exiting lets restartPolicy=ON_FAILURE retry.
                os._exit(1)

        thread = threading.Thread(target=run, name="model-loader", daemon=True)
        thread.start()
        return thread

    def transcribe(self, path: str, language: str = "auto") -> dict:
        """Returns {"text", "language", "segments": [{"start", "end", "text"}]}."""
        with MODEL_LOCK:
            segments_iter, info = self.model.transcribe(
                path,
                language=None if language == "auto" else language,
                vad_filter=True,  # skip silence: less CPU, fewer hallucinated words
            )
            # The model runs lazily as the generator is consumed, so consume it inside the lock.
            segments = [
                {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()}
                for seg in segments_iter
            ]
        text = " ".join(seg["text"] for seg in segments if seg["text"])
        return {"text": text, "language": info.language, "segments": segments}
