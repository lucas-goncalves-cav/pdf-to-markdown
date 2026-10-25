#!/usr/bin/env bash
# The time limit end to end, in the image, under the restrictions the
# Automation Core runs it with: read only root, a 512 MB tmpfs on /tmp, no
# capabilities, no new privileges, 2 GB, 1.5 CPUs, 256 pids, user 1001.
#
# A scan that takes longer than the limit must be answered with a 504 while
# tesseract is running, leave nothing in the process table or in /tmp, and the
# service must go on converting.
#
#   scripts/smoke_timeout.sh pdf-to-markdown:local
#
# SMOKE_LIMIT sets the limit for the run (20 seconds), SMOKE_PORT the host port
# (18000), SMOKE_WORKDIR where the generated PDFs go (a new temporary directory).
set -euo pipefail

image="${1:?usage: $0 IMAGE}"
limit="${SMOKE_LIMIT:-20}"
port="${SMOKE_PORT:-18000}"
work="${SMOKE_WORKDIR:-$(mktemp -d)}"
name="pdf-to-markdown-smoke-$$"
url="http://127.0.0.1:$port"

mkdir -p "$work"

cleanup() {
  docker rm -f "$name" > /dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "FAILED: $*" >&2
  docker logs "$name" 2>&1 | tail -30 >&2 || true
  exit 1
}

# The limits are the Core's compose defaults. No healthcheck: its curl would
# show up in the process table this checks.
docker run -d --name "$name" \
  --read-only --tmpfs /tmp:size=512m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 2g --cpus 1.5 --pids-limit 256 --user 1001 \
  --no-healthcheck \
  -e CONVERSION_TIMEOUT_SECONDS="$limit" \
  -e MAX_UPLOAD_BYTES=31457280 -e MAX_PAGES=300 \
  -e OCR_ENABLED=true -e OCR_LANG=por+eng -e OCR_MAX_PAGES=30 \
  -p "127.0.0.1:$port:8000" \
  "$image" > /dev/null

for _ in $(seq 1 60); do
  curl -fsS "$url/health" > /dev/null 2>&1 && break
  sleep 1
done

curl -fsS "$url/health" > /dev/null || fail "the service did not come up"
curl -fsS "$url/api/limits" | grep -q "\"conversionTimeoutSeconds\":$limit" \
  || fail "/api/limits does not report the limit"

# Scans built with the image's own Pillow, so the smoke needs nothing on the
# host but docker and curl: pictures of pages of text, which only OCR can read.
# With 1.5 CPUs tesseract takes ten seconds or more on a full page, so six of
# them run well past the limit, and the limit leaves time for tesseract to start.
scan() {
  docker exec -i "$name" python - "$1" "$2" << 'PYTHON'
import io
import sys

from PIL import Image, ImageDraw, ImageFont

font = ImageFont.load_default(size=36)
pages = []

for number in range(int(sys.argv[1])):
    page = Image.new("L", (2480, 3508), 255)
    draw = ImageDraw.Draw(page)
    draw.text((200, 200), f"EDITAL DE ABERTURA {number + 1}", font=ImageFont.load_default(size=64))

    for line in range(int(sys.argv[2])):
        draw.text(
            (200, 400 + line * 50),
            f"{line + 1}. O candidato deve apresentar todos os documentos exigidos neste edital.",
            font=font,
        )

    pages.append(page)

buffer = io.BytesIO()
pages[0].save(buffer, "PDF", resolution=300, save_all=True, append_images=pages[1:])
sys.stdout.buffer.write(buffer.getvalue())
PYTHON
}

scan 6 60 > "$work/long-scan.pdf"
scan 1 12 > "$work/short-scan.pdf"

# The process table of the container, from /proc, since the image has no ps.
table() {
  docker exec -i "$name" python - << 'PYTHON'
import os
from pathlib import Path

me = os.getpid()

for entry in sorted(Path("/proc").iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
    if not entry.name.isdigit() or int(entry.name) == me:
        continue

    try:
        stat = (entry / "stat").read_text()
    except OSError:
        continue

    name = stat[stat.index("(") + 1 : stat.rindex(")")]
    state, ppid, pgid = stat[stat.rindex(")") + 2 :].split()[:3]
    print(f"{entry.name} ppid={ppid} pgid={pgid} state={state} {name}")
PYTHON
}

# Sample the table while the long scan converts, to prove tesseract was
# running when the limit hit and the stop is not just a slow start.
rm -f "$work/done"
(
  while [ ! -e "$work/done" ]; do
    table | grep -c " tesseract$" || true
    sleep 0.25
  done
) > "$work/tesseract-samples.txt" 2> /dev/null &
sampler=$!

started=$(date +%s)
status=$(curl -s -o "$work/timeout.json" -w '%{http_code}' -X POST "$url/api/normalized-document" \
  -H "X-Correlation-Id: smoke-timeout-1" \
  -F "file=@$work/long-scan.pdf;type=application/pdf")
elapsed=$(($(date +%s) - started))
touch "$work/done"
wait "$sampler" || true

echo "Long scan: HTTP $status after ${elapsed}s, limit ${limit}s"
cat "$work/timeout.json"
echo

[ "$status" = "504" ] || fail "expected 504, got $status"
grep -q '"code":"conversion_timeout"' "$work/timeout.json" || fail "no conversion_timeout code"
[ "$elapsed" -ge "$limit" ] || fail "answered before the limit"
# The upload and the sampling above take their share of 1.5 CPUs; the full
# conversion would take minutes.
[ "$elapsed" -le $((limit + 10)) ] || fail "answered ${elapsed}s after a ${limit}s limit"

most=$(sort -n "$work/tesseract-samples.txt" | tail -1)
[ "${most:-0}" -ge 1 ] || fail "tesseract was never seen running, so the stop proved nothing"
echo "tesseract seen running during the conversion: up to $most at once"

after=$(table)
echo "Process table after the timeout:"
echo "$after"

[ "$(echo "$after" | wc -l)" -eq 1 ] || fail "something besides the server is left"
echo "$after" | grep -q "^1 .* state=[SR] " || fail "the server is not PID 1, or not running"

leftovers=$(docker exec "$name" ls -A /tmp)
[ -z "$leftovers" ] || fail "/tmp is not empty: $leftovers"
echo "/tmp is empty"

docker logs "$name" 2>&1 | grep -q "Stopped a conversion.*correlation_id=smoke-timeout-1" \
  || fail "the timeout is not in the log with its correlation id"

curl -fsS "$url/health" > /dev/null || fail "/health fails after the timeout"

status=$(curl -s -o "$work/short.json" -w '%{http_code}' -X POST "$url/api/normalized-document" \
  -H "X-Correlation-Id: smoke-timeout-2" \
  -F "file=@$work/short-scan.pdf;type=application/pdf")

[ "$status" = "200" ] || fail "a short scan after the timeout got $status"
grep -q '"OCR_USED"' "$work/short.json" || fail "the short scan was not read with OCR"
grep -q 'O candidato deve apresentar' "$work/short.json" || fail "the short scan text is missing"
echo "Short scan after the timeout: HTTP 200, read with OCR"

[ "$(table | wc -l)" -eq 1 ] || fail "the short scan left a process behind"
[ -z "$(docker exec "$name" ls -A /tmp)" ] || fail "the short scan left files in /tmp"

echo "Time limit smoke passed."
