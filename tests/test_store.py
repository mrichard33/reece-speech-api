"""SupabaseStore request shapes, checked against a fake HTTP transport (no network)."""

import json

import httpx
import pytest

from app.store import StoreError, SupabaseStore


def make_store(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SupabaseStore("https://proj.supabase.co/", "service-key", client=client)


def test_create_and_read_job():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET" and request.url.path.endswith("/speech_jobs"):
            return httpx.Response(200, json=[{"id": "j1", "status": "queued", "chunks_total": 1}])
        if request.method == "GET":
            return httpx.Response(200, json=[{"chunk_index": 0, "status": "queued"}])
        return httpx.Response(201)

    store = make_store(handler)
    store.create_job({"id": "j1", "status": "queued"}, [{"chunk_index": 0, "start_seconds": 0, "end_seconds": 5}])
    job = store.get_job("j1")

    assert [r.url.path for r in seen[:2]] == ["/rest/v1/speech_jobs", "/rest/v1/speech_job_chunks"]
    assert json.loads(seen[1].content)[0]["job_id"] == "j1"
    assert seen[0].headers["apikey"] == "service-key"
    assert seen[0].headers["authorization"] == "Bearer service-key"
    assert job["chunks"] == [{"chunk_index": 0, "status": "queued"}]


def test_fail_unfinished_counts_rows():
    def handler(request):
        assert request.method == "PATCH"
        assert request.url.params["status"] == "in.(queued,processing)"
        assert json.loads(request.content)["status"] == "failed"
        return httpx.Response(200, json=[{"id": "a"}, {"id": "b"}])

    assert make_store(handler).fail_unfinished("restarted") == 2


def test_http_error_becomes_store_error():
    store = make_store(lambda request: httpx.Response(404, json={"message": "relation does not exist"}))
    with pytest.raises(StoreError):
        store.get_job("j1")
