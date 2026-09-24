import pytest

from app.auth import is_authorized
from app.config import ConfigError, load_settings
from tests.conftest import AUTH, FakeTranscriber, make_audio


def test_no_key_is_401(build_client):
    client = build_client()
    assert client.get("/v1/jobs/00000000-0000-0000-0000-000000000000").status_code == 401
    assert client.post("/v1/transcribe").status_code == 401


def test_wrong_key_is_401(build_client):
    client = build_client()
    for header in ("Bearer nope", "Basic test-key", "test-key", "Bearer "):
        r = client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers={"Authorization": header})
        assert r.status_code == 401, header


def test_right_key_passes(build_client, tmp_path):
    client = build_client()
    r = client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=AUTH)
    assert r.status_code == 404  # got past auth; the job just doesn't exist

    wav = make_audio(tmp_path / "a.wav", 2)
    with open(wav, "rb") as f:
        r = client.post("/v1/transcribe", headers=AUTH, files={"audio": ("a.wav", f)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "small" and body["language"] == "en"
    assert body["segments"] == [{"start": 0.0, "end": 1.0, "text": "part 1"}]
    assert 1.9 < body["duration"] < 2.1


def test_health_needs_no_key(build_client):
    r = build_client().get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "model": "small", "compute_type": "int8", "model_loaded": True}


def test_health_is_503_until_model_loaded(build_client):
    client = build_client(transcriber=FakeTranscriber(loaded=False))
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["model_loaded"] is False
    assert client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=AUTH).status_code == 503


def test_constant_time_helper():
    assert is_authorized("Bearer abc", "abc")
    assert is_authorized("bearer abc", "abc")
    assert not is_authorized(None, "abc")
    assert not is_authorized("Bearer abcd", "abc")


def test_service_refuses_to_start_without_key():
    with pytest.raises(ConfigError):
        load_settings({})
    with pytest.raises(ConfigError):
        load_settings({"SPEECH_API_KEY": "   "})


def test_supabase_without_credentials_falls_back_to_memory():
    s = load_settings({"SPEECH_API_KEY": "k", "JOB_STORE": "supabase"})
    assert s.job_store == "memory"
