"""Getting audio onto local disk, checking it, normalising it, and cutting it into chunks.

Every file this module writes lives inside a per-request temp dir that the caller deletes
in a `finally` block. Audio is never written anywhere else.
"""

import ipaddress
import json
import math
import os
import socket
import subprocess
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

READ_PIECE = 1024 * 1024  # stream uploads/downloads 1 MB at a time

# Checked against what ffprobe says the content IS, never the file extension.
# m4a / m4b / mp4 all report as "mov,mp4,m4a,3gp,3g2,mj2"; opus lives in "ogg";
# webm reports as "matroska,webm"; raw AAC (ADTS) reports as "aac".
ALLOWED_FORMATS = {"mp3", "mov", "mp4", "m4a", "aac", "wav", "flac", "ogg", "matroska", "webm"}

# A tail shorter than this is folded into the previous chunk instead of becoming its own
# near-empty chunk (e.g. 3600.2 s at 1800 s chunks is 2 chunks, not 3).
MIN_TAIL_SECONDS = 1.0

MAX_REDIRECTS = 5
FFPROBE_TIMEOUT = 120


class AudioError(Exception):
    """A problem with the caller's audio, mapped straight to an HTTP status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class AudioInfo:
    format_name: str
    duration: float | None  # None when the container header does not say (common for webm)


@dataclass(frozen=True)
class Chunk:
    index: int
    start: float
    end: float


# ---------------------------------------------------------------- getting the audio


def _too_large(max_bytes: int) -> AudioError:
    return AudioError(413, f"Audio is larger than the {max_bytes // (1024 * 1024)} MB limit")


async def save_upload(upload, dest_path: str, max_bytes: int) -> int:
    """Copy an UploadFile to dest_path, refusing anything over max_bytes."""
    written = 0
    with open(dest_path, "wb") as out:
        while True:
            piece = await upload.read(READ_PIECE)
            if not piece:
                break
            written += len(piece)
            if written > max_bytes:
                raise _too_large(max_bytes)
            out.write(piece)
    if written == 0:
        raise AudioError(400, "Uploaded audio file is empty")
    return written


def _check_public_host(host: str | None) -> None:
    """Refuse URLs that point at our own network (Railway private net, metadata, localhost).

    Without this, anyone holding the key could make the service fetch internal addresses.
    """
    if not host:
        raise AudioError(400, "audio_url has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise AudioError(400, f"audio_url host could not be resolved: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise AudioError(400, "audio_url must point to a public internet address")


def download_url(url: str, dest_path: str, max_bytes: int, client: httpx.Client | None = None) -> int:
    """Download audio_url to dest_path with the same size cap as uploads."""
    own_client = client is None
    client = client or httpx.Client(follow_redirects=False, timeout=httpx.Timeout(30.0, read=120.0))
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                raise AudioError(400, "audio_url must start with http:// or https://")
            # Checked on every hop, so a public URL cannot redirect us to an internal one.
            _check_public_host(parsed.hostname)
            try:
                with client.stream("GET", url) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise AudioError(400, "audio_url redirected without a location")
                        url = urljoin(url, location)
                        continue
                    if resp.status_code >= 400:
                        raise AudioError(400, f"audio_url returned HTTP {resp.status_code}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise _too_large(max_bytes)
                    written = 0
                    with open(dest_path, "wb") as out:
                        for piece in resp.iter_bytes(READ_PIECE):
                            written += len(piece)
                            if written > max_bytes:
                                raise _too_large(max_bytes)
                            out.write(piece)
                    if written == 0:
                        raise AudioError(400, "audio_url returned an empty file")
                    return written
            except httpx.HTTPError as exc:
                raise AudioError(400, f"audio_url could not be downloaded ({type(exc).__name__})") from exc
        raise AudioError(400, "audio_url redirected too many times")
    finally:
        if own_client:
            client.close()


# ---------------------------------------------------------------- inspecting and converting


def probe(path: str) -> AudioInfo:
    """Identify the audio by content. Unknown or non-audio content -> 415."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(415, "Audio could not be read") from exc
    unsupported = AudioError(
        415, "Unsupported audio format. Allowed: mp3, m4a, m4b, aac, wav, flac, ogg, opus, webm, mp4"
    )
    if proc.returncode != 0:
        raise unsupported
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise unsupported from exc
    fmt = data.get("format") or {}
    format_name = fmt.get("format_name", "")
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams") or [])
    if not has_audio or not (set(format_name.split(",")) & ALLOWED_FORMATS):
        raise unsupported
    duration = None
    try:
        value = float(fmt.get("duration"))
        if math.isfinite(value) and value > 0:
            duration = value
    except (TypeError, ValueError):
        pass
    return AudioInfo(format_name=format_name, duration=duration)


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AudioError(415, "Audio could not be decoded")


def normalize(src: str, dst: str) -> None:
    """Convert to mono, 16 kHz, 16-bit WAV — what Whisper wants, and the cheapest to decode.

    `-rf64 auto` lets files past the 4 GB WAV limit (~37 h of audio) still be written.
    """
    _ffmpeg(["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-rf64", "auto", dst])


def wav_duration(path: str) -> float:
    """Exact duration of a normalised WAV."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
    )
    try:
        return max(0.0, float(json.loads(proc.stdout)["format"]["duration"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AudioError(415, "Audio could not be decoded") from exc


def plan_chunks(duration: float, chunk_seconds: float) -> list[Chunk]:
    """Split [0, duration] into chunk_seconds pieces. Pure — no files involved."""
    if duration <= chunk_seconds:
        return [Chunk(0, 0.0, max(0.0, duration))]
    count = math.ceil(duration / chunk_seconds)
    if count > 1 and duration - (count - 1) * chunk_seconds < MIN_TAIL_SECONDS:
        count -= 1
    chunks = []
    for i in range(count):
        start = i * chunk_seconds
        end = duration if i == count - 1 else (i + 1) * chunk_seconds
        chunks.append(Chunk(i, float(start), float(end)))
    return chunks


def extract_chunk(wav: str, chunk: Chunk, is_last: bool, dst: str) -> None:
    """Cut one chunk out of the normalised WAV.

    The last chunk runs to the end of the file rather than to its planned end, so a small
    gap between the header duration and the decoded duration never drops audio.
    """
    args = ["-ss", f"{chunk.start:.3f}", "-i", wav]
    if not is_last:
        args += ["-t", f"{chunk.end - chunk.start:.3f}"]
    # Re-writing PCM is nearly free and cuts on the exact sample; "-c copy" snaps to packet
    # boundaries (~64 ms here), which would shift every timestamp after the cut.
    _ffmpeg(args + ["-c:a", "pcm_s16le", dst])


def remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
