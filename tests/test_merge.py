import glob
import os
import tempfile

from app.jobs import RESTART_MESSAGE, merge_chunks
from app.store import MemoryStore
from tests.conftest import AUTH, FakeTranscriber, make_audio, wait_for_job


def test_offset_is_added_to_later_chunks():
    merged = merge_chunks([
        (0.0, {"text": "Good morning,", "language": "en",
               "segments": [{"start": 0.0, "end": 4.8, "text": "Good morning,"}]}),
        (1800.0, {"text": " this is Mark with Reece Windows. ", "language": "en",
                  "segments": [{"start": 0.0, "end": 4.0, "text": "this is Mark with Reece Windows."}]}),
    ])
    assert merged["segments"][1] == {"start": 1800.0, "end": 1804.0, "text": "this is Mark with Reece Windows."}
    assert merged["segments"][0] == {"start": 0.0, "end": 4.8, "text": "Good morning,"}
    assert merged["text"] == "Good morning, this is Mark with Reece Windows."
    assert merged["language"] == "en"


def test_empty_chunks_do_not_leave_double_spaces():
    merged = merge_chunks([(0.0, {"text": "a", "segments": []}), (10.0, {"text": "", "segments": []}),
                           (20.0, {"text": "b", "segments": []})])
    assert merged["text"] == "a b"


def submit(client, path, **data):
    with open(path, "rb") as f:
        r = client.post("/v1/jobs", headers=AUTH, files={"audio": (path.name, f)}, data=data)
    assert r.status_code == 202, r.text
    return r.json()


def test_job_end_to_end_offsets(build_client, tmp_path):
    client = build_client(chunk_seconds=3)
    clip = make_audio(tmp_path / "seven.m4a", 7, fmt_args=("-c:a", "aac"))
    created = submit(client, clip)
    assert created["status"] == "queued" and created["chunks_total"] == 3

    body = wait_for_job(client, created["job_id"])
    assert body["status"] == "done", body
    assert body["chunks_done"] == 3
    assert body["chunks"] == [{"index": i, "status": "done"} for i in range(3)]
    result = body["result"]
    assert [s["start"] for s in result["segments"]] == [0.0, 3.0, 6.0]
    assert result["text"] == "part 1 part 2 part 3"
    assert set(result) == {"text", "language", "duration", "model", "segments"}


def test_chunk_retries_then_succeeds(build_client, tmp_path):
    fake = FakeTranscriber(fail_times=2)
    client = build_client(transcriber=fake)
    body = wait_for_job(client, submit(client, make_audio(tmp_path / "a.wav", 2))["job_id"])
    assert body["status"] == "done" and len(fake.calls) == 3


def test_chunk_fails_after_three_attempts_and_finished_chunks_are_not_rerun(build_client, tmp_path):
    class FailSecondChunk(FakeTranscriber):
        def transcribe(self, path, language="auto"):
            if path.endswith("chunk_1.wav"):
                self.calls.append(path)
                raise RuntimeError("boom")
            return super().transcribe(path, language)

    fake = FailSecondChunk()
    client = build_client(transcriber=fake, chunk_seconds=3)
    body = wait_for_job(client, submit(client, make_audio(tmp_path / "a.wav", 7))["job_id"])
    assert body["status"] == "failed"
    assert "chunk 1" in body["error"]
    assert [c["status"] for c in body["chunks"]] == ["done", "failed", "queued"]
    assert sum(p.endswith("chunk_0.wav") for p in fake.calls) == 1  # done chunk never re-run
    assert sum(p.endswith("chunk_1.wav") for p in fake.calls) == 3


def test_temp_audio_is_deleted_after_success_and_failure(build_client, tmp_path):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "speech-*")))
    ok = build_client()
    wait_for_job(ok, submit(ok, make_audio(tmp_path / "a.wav", 1))["job_id"])
    bad = build_client(transcriber=FakeTranscriber(fail_times=99))
    assert wait_for_job(bad, submit(bad, make_audio(tmp_path / "b.wav", 1))["job_id"])["status"] == "failed"
    with open(make_audio(tmp_path / "c.wav", 1), "rb") as f:
        ok.post("/v1/transcribe", headers=AUTH, files={"audio": ("c.wav", f)})
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "speech-*"))) == before


def test_restart_marks_unfinished_jobs_failed(build_client):
    store = MemoryStore()
    for job_id, status in (("11111111-1111-1111-1111-111111111111", "queued"),
                           ("22222222-2222-2222-2222-222222222222", "processing"),
                           ("33333333-3333-3333-3333-333333333333", "done")):
        store.create_job({"id": job_id, "status": status, "chunks_total": 1, "chunks_done": 0}, [])
    client = build_client(store=store)  # startup runs the restart sweep
    views = {j: client.get(f"/v1/jobs/{j}", headers=AUTH).json() for j in
             ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
              "33333333-3333-3333-3333-333333333333")}
    assert views["11111111-1111-1111-1111-111111111111"]["error"] == RESTART_MESSAGE
    assert views["22222222-2222-2222-2222-222222222222"]["status"] == "failed"
    assert views["33333333-3333-3333-3333-333333333333"]["status"] == "done"


def test_unknown_job_is_404(build_client):
    client = build_client()
    assert client.get("/v1/jobs/not-a-uuid", headers=AUTH).status_code == 404
    assert client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=AUTH).status_code == 404
