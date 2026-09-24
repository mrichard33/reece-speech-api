from tests.conftest import AUTH, make_audio


def post_file(client, path, route="/v1/transcribe", **data):
    with open(path, "rb") as f:
        return client.post(route, headers=AUTH, files={"audio": (path.name, f)}, data=data)


def test_oversize_upload_is_413(build_client, tmp_path):
    client = build_client(max_upload_mb=1)
    big = make_audio(tmp_path / "big.wav", 40)  # ~3.5 MB of 44.1 kHz PCM
    assert post_file(client, big).status_code == 413
    assert post_file(client, big, route="/v1/jobs").status_code == 413


def test_oversize_upload_without_content_length_is_413(tmp_path):
    """The streaming copy enforces the cap even when no Content-Length header is sent."""
    import asyncio
    import io

    import pytest

    from app.audio import AudioError, save_upload

    class Upload:
        def __init__(self, data):
            self.buf = io.BytesIO(data)

        async def read(self, n):
            return self.buf.read(n)

    with pytest.raises(AudioError) as exc:
        asyncio.run(save_upload(Upload(b"x" * (3 * 1024 * 1024)), str(tmp_path / "out"), 2 * 1024 * 1024))
    assert exc.value.status_code == 413


def test_bad_format_is_415(build_client, tmp_path):
    client = build_client()
    fake = tmp_path / "notes.mp3"  # the extension lies; content decides
    fake.write_text("this is not audio at all\n" * 100)
    r = post_file(client, fake)
    assert r.status_code == 415
    assert post_file(client, fake, route="/v1/jobs").status_code == 415


def test_sync_over_max_seconds_is_413(build_client, tmp_path):
    client = build_client(sync_max_seconds=1)
    clip = make_audio(tmp_path / "three.wav", 3)
    r = post_file(client, clip)
    assert r.status_code == 413
    assert "Use POST /v1/jobs for long audio" in r.json()["detail"]


def test_rate_limit_is_429(build_client):
    client = build_client(rate_limit_per_min=2)
    url = "/v1/jobs/00000000-0000-0000-0000-000000000000"
    assert client.get(url, headers=AUTH).status_code == 404
    assert client.get(url, headers=AUTH).status_code == 404
    assert client.get(url, headers=AUTH).status_code == 429


def test_needs_exactly_one_source(build_client, tmp_path):
    client = build_client()
    assert client.post("/v1/transcribe", headers=AUTH, data={"language": "en"}).status_code == 400
    clip = make_audio(tmp_path / "a.wav", 1)
    r = post_file(client, clip, audio_url="https://example.com/a.mp3")
    assert r.status_code == 400


def test_audio_url_to_internal_address_is_refused(build_client):
    client = build_client()
    for url in ("http://127.0.0.1/a.mp3", "http://169.254.169.254/latest", "ftp://example.com/a.mp3"):
        r = client.post("/v1/transcribe", headers=AUTH, data={"audio_url": url})
        assert r.status_code == 400, url


def test_unknown_language_is_400(build_client, tmp_path):
    clip = make_audio(tmp_path / "a.wav", 1)
    assert post_file(build_client(), clip, language="klingon").status_code == 400


def test_timestamps_false_returns_empty_segments(build_client, tmp_path):
    clip = make_audio(tmp_path / "a.mp3", 2)
    r = post_file(build_client(), clip, timestamps="false")
    assert r.status_code == 200, r.text
    assert r.json()["segments"] == [] and r.json()["text"] == "part 1"
