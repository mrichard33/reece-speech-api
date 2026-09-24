#!/usr/bin/env bash
# Smoke test a running Reece Speech API with a real audio file.
# Usage: ./scripts/smoke_test.sh https://<domain> <SPEECH_API_KEY> path/to/call.mp3
set -euo pipefail

if [ $# -ne 3 ]; then
  echo "Usage: $0 <base_url> <api_key> <audio_file>" >&2
  exit 2
fi
BASE="${1%/}"
KEY="$2"
FILE="$3"
[ -f "$FILE" ] || { echo "File not found: $FILE" >&2; exit 2; }

echo "== 1. Health check"
curl -sS "$BASE/health"
echo

echo "== 2. Transcribe (waits for the result)"
code=$(curl -sS -o /tmp/speech_smoke.json -w '%{http_code}' \
  -H "Authorization: Bearer $KEY" \
  -F "audio=@$FILE" \
  -F "language=auto" \
  -F "timestamps=true" \
  "$BASE/v1/transcribe")
echo "HTTP $code"
cat /tmp/speech_smoke.json
echo
rm -f /tmp/speech_smoke.json

if [ "$code" = "413" ]; then
  echo "File is too long for the quick route. Submit it as a job instead:"
  echo "  curl -H \"Authorization: Bearer <key>\" -F audio=@$FILE $BASE/v1/jobs"
  echo "  curl -H \"Authorization: Bearer <key>\" $BASE/v1/jobs/<job_id>"
fi
[ "$code" = "200" ]
