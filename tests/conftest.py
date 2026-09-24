"""Shared test helpers. The real Whisper model is never loaded: a fake stands in for it."""

import os
import subprocess
import threading
import time

os.environ.setdefault("SPEECH_API_KEY", "test-key")  # app.main builds an app at import time

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.store import MemoryStore

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


class FakeTranscriber:
    """Returns one 0.0–1.0 segment per call. `fail_times` makes the first N calls raise."""

    def __init__(self, fail_times: int = 0, loaded: bool = True):
        self.loaded = loaded
        self.fail_times = fail_times
        self.calls = []
        self._lock = threading.Lock()

    def transcribe(self, path, language="auto"):
        with self._lock:
            self.calls.append(path)
            if len(self.calls) <= self.fail_times:
                raise RuntimeError("simulated model failure")
            n = len(self.calls)
        return {"text": f"part {n}", "language": "en",
                "segments": [{"start": 0.0, "end": 1.0, "text": f"part {n}"}]}


def make_audio(path, seconds, kind="sine", fmt_args=()):
    """Generate test audio with ffmpeg — no audio files are committed to the repo."""
    source = f"sine=frequency=440:duration={seconds}" if kind == "sine" else "anullsrc=r=8000:cl=mono"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source]
    if kind != "sine":
        cmd += ["-t", str(seconds)]
    subprocess.run(cmd + list(fmt_args) + [str(path)], check=True)
    return path


@pytest.fixture
def build_client():
    """build_client(**env_overrides, transcriber=..., store=...) -> TestClient (lifespan running)."""
    clients = []

    def _build(transcriber=None, store=None, **env):
        full_env = {"SPEECH_API_KEY": KEY, "MODEL_CACHE_DIR": "/tmp/unused-models",
                    **{k.upper(): str(v) for k, v in env.items()}}
        app = create_app(load_settings(full_env), transcriber=transcriber or FakeTranscriber(),
                         store=store or MemoryStore())
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _build
    for c in clients:
        c.__exit__(None, None, None)


def wait_for_job(client, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {body}")
