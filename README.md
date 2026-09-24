# Reece Speech API

A private service that turns audio into text, with timestamps. Any Reece app (Five9 calls,
sales recordings, Omi, meetings, audiobooks, voice notes) can send it audio and get words back.

- **No paid AI.** Speech-to-text runs on [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
  (`small` model, `int8`, CPU) inside our own container. Railway compute is the only cost.
- **Audio is never stored.** Files live in a temp folder only while being transcribed, then are
  deleted — after success and after failure.
- **Speech only.** No summaries or CRM extraction here; that will be a separate service.

## How to call it

Every route except `/health` needs this header:

```
Authorization: Bearer <SPEECH_API_KEY>
```

### Is it up? — `GET /health` (no key needed)

```json
{"status":"ok","model":"small","compute_type":"int8","model_loaded":true}
```

Returns **503** until the model has finished loading (a few minutes on the very first boot).

### Short audio (up to 15 min) — `POST /v1/transcribe`

Waits and returns the text.

```bash
curl -H "Authorization: Bearer $KEY" \
  -F audio=@call.mp3 \
  https://<domain>/v1/transcribe
```

Form fields:

| field | meaning |
|---|---|
| `audio` | the audio file — **or** — |
| `audio_url` | a public http(s) link to the audio (send exactly one of the two) |
| `language` | `auto` (default) or a code like `en`, `es` |
| `timestamps` | `true` (default) or `false` (then `segments` is `[]`) |

```json
{
  "text": "Good morning, this is Mark with Reece Windows...",
  "language": "en",
  "duration": 184.2,
  "model": "small",
  "segments": [{"start": 0.0, "end": 4.8, "text": "Good morning, this is Mark with Reece Windows."}]
}
```

### Long audio — `POST /v1/jobs`, then `GET /v1/jobs/{job_id}`

Same fields as above, no length cap. Returns right away:

```json
{"job_id":"<uuid>","status":"queued","chunks_total":20}
```

Then check on it every 10–15 seconds:

```json
{
  "job_id": "...", "status": "processing",
  "chunks_total": 20, "chunks_done": 13,
  "chunks": [{"index":0,"status":"done"}, {"index":13,"status":"processing"}],
  "result": null, "error": null
}
```

When `status` is `done`, `result` has the same shape as the `/v1/transcribe` answer.
If it is `failed`, `error` says why (for example which chunk failed).

With the default memory store, jobs are forgotten when the service restarts and finished jobs
are dropped after 24 hours. Jobs that were running during a restart are marked `failed` with
"Service restarted — please resubmit".

### Errors

| code | why |
|---|---|
| 400 | not exactly one of `audio` / `audio_url`, bad URL, unknown language |
| 401 | missing or wrong API key |
| 404 | unknown job id |
| 413 | file over `MAX_UPLOAD_MB`, or audio over `SYNC_MAX_SECONDS` on `/v1/transcribe` (use `/v1/jobs`) |
| 415 | not a supported audio format: mp3, m4a, m4b, aac, wav, flac, ogg, opus, webm, mp4 (checked by content, not the file name) |
| 429 | more than `RATE_LIMIT_PER_MIN` requests in a minute |
| 503 | model still loading, or the job store is unreachable |

## How it works

1. The model loads **once** when the service starts (cached on the `/models` volume).
2. Audio is converted with ffmpeg to mono, 16 kHz WAV.
3. Long audio is cut into 30-minute pieces. Each piece is transcribed, its timestamps are shifted
   by where the piece starts, and the text is joined together.
4. A piece that fails is retried up to 2 more times. Finished pieces are never redone.
5. Only one transcription runs at a time (sync requests and jobs share the model), which keeps
   CPU and memory flat.
6. Silence is skipped (`vad_filter`), which saves CPU.

Logs show one line per request/job: id, audio length, seconds taken, model. Never audio or text.

## Settings (environment variables)

See `.env.example`. Only `SPEECH_API_KEY` is required.

| variable | default | what it does |
|---|---|---|
| `SPEECH_API_KEY` | — | **required**; the service will not start without it |
| `WHISPER_MODEL` | `small` | raise to `medium` if accuracy isn't good enough |
| `WHISPER_COMPUTE_TYPE` | `int8` | |
| `WHISPER_DEVICE` | `cpu` | |
| `WHISPER_CPU_THREADS` | `0` | 0 = automatic |
| `MODEL_CACHE_DIR` | `/models` | where model files are kept (mount a volume here) |
| `MAX_UPLOAD_MB` | `1024` | largest file accepted |
| `SYNC_MAX_SECONDS` | `900` | longest audio for `/v1/transcribe` |
| `CHUNK_SECONDS` | `1800` | piece length for long audio |
| `JOB_WORKERS` | `1` | background job workers |
| `RATE_LIMIT_PER_MIN` | `30` | per API key |
| `JOB_STORE` | `memory` | `supabase` keeps jobs across restarts |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | — | only for `JOB_STORE=supabase`; if missing, it falls back to memory |
| `PORT` | set by Railway | |

To use Supabase: run the three statements in `sql/001_speech_jobs.sql` in the LP MCP Supabase
dashboard (separately, in order), then set `JOB_STORE=supabase` plus the two Supabase variables.

## Running locally

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt        # also needs ffmpeg installed
pytest -q                                   # model is mocked; nothing is downloaded
SPEECH_API_KEY=dev MODEL_CACHE_DIR=./models uvicorn app.main:app --port 8000
./scripts/smoke_test.sh http://localhost:8000 dev path/to/call.mp3
```
