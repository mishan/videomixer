#!/usr/bin/env bash
#
# End-to-end check: publish two test patterns, mix them, pull the result back
# and assert it actually contains H.264 video *and* AAC audio.
#
# Assumes the compose stack is up:
#   docker compose up --build -d
#
# The mixer runs inside the compose network and reaches the RTMP server as
# "rtmp", while this script reaches it as localhost -- hence the two sets of
# URLs below.
set -euo pipefail

API="${API:-http://localhost:8888}"
RTMP_HOST_URL="${RTMP_HOST_URL:-rtmp://localhost:1935/live}"
RTMP_NET_URL="${RTMP_NET_URL:-rtmp://rtmp:1935/live}"
STREAM_ID="${STREAM_ID:-e2e}"
WORKDIR="$(mktemp -d)"
PIDS=()

cleanup() {
    local status=$?
    for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
    curl -sf -X DELETE "$API/stream/$STREAM_ID" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
    exit $status
}
trap cleanup EXIT

need() { command -v "$1" >/dev/null || { echo "error: $1 is required" >&2; exit 1; }; }
need ffmpeg
need ffprobe
need curl

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

publish() {  # publish <name> <tone-hz> <size> <pattern>
    ffmpeg -nostdin -loglevel error -re \
        -f lavfi -i "aevalsrc=sin($2*2*PI*t):s=44100" \
        -f lavfi -i "$4=size=$3:rate=30" \
        -c:v libx264 -preset ultrafast -tune zerolatency -b:v 500k \
        -c:a aac -b:a 64k -vf format=yuv420p \
        -x264opts keyint=30:min-keyint=30:scenecut=-1 \
        -f flv "$RTMP_HOST_URL/$1" >"$WORKDIR/$1.log" 2>&1 &
    PIDS+=($!)
}

say "Waiting for the mixer API"
for i in $(seq 30); do
    curl -sf "$API/health" >/dev/null && break
    [ "$i" = 30 ] && fail "API never came up at $API"
    sleep 1
done
pass "API is healthy"

say "Publishing source streams"
publish testpattern 400 640x360 testsrc
publish cam         900 320x180 smptebars
sleep 5
pass "two publishers running"

say "Creating mixed stream"
curl -sf -H 'Content-Type: application/json' -X PUT \
    -d "{\"bg_uri\":\"$RTMP_NET_URL/testpattern\",
         \"output_uri\":\"$RTMP_NET_URL/mixed\",
         \"width\":640,\"height\":360}" \
    "$API/stream/$STREAM_ID" | tee "$WORKDIR/create.json"
grep -q '"status": "OK"' "$WORKDIR/create.json" || fail "could not create stream"
pass "stream created"
sleep 5

say "Adding a picture-in-picture to the running stream"
curl -sf -H 'Content-Type: application/json' -X PUT \
    -d "{\"stream_uri\":\"$RTMP_NET_URL/cam\",
         \"x\":400,\"y\":220,\"z\":10,\"width\":200,\"height\":112}" \
    "$API/stream/$STREAM_ID/cam1" | tee "$WORKDIR/pip.json"
grep -q '"status": "OK"' "$WORKDIR/pip.json" || fail "could not add PiP"
pass "PiP added without restarting the pipeline"
sleep 8

say "Recording the mixed output"
ffmpeg -nostdin -loglevel error -i "$RTMP_HOST_URL/mixed" -t 5 -c copy \
    -y "$WORKDIR/mixed.flv" >"$WORKDIR/record.log" 2>&1 \
    || fail "could not pull the mixed stream (see $WORKDIR/record.log)"
[ -s "$WORKDIR/mixed.flv" ] || fail "mixed output is empty -- the pipeline is stalled"
pass "recorded $(stat -c%s "$WORKDIR/mixed.flv") bytes"

say "Verifying the output"
probe=$(ffprobe -v error -show_entries stream=codec_type,codec_name \
        -of csv=p=0 "$WORKDIR/mixed.flv")
echo "$probe" | sed 's/^/  /'

echo "$probe" | grep -q '^h264,video' || fail "no H.264 video track"
pass "H.264 video present"

# This is the one that used to fail: audio would freeze the whole pipeline.
echo "$probe" | grep -q '^aac,audio'  || fail "no AAC audio track"
pass "AAC audio present"

mean=$(ffmpeg -nostdin -v error -i "$WORKDIR/mixed.flv" -af volumedetect \
       -f null /dev/null 2>&1 | grep -oE 'mean_volume: [-0-9.]+' | cut -d' ' -f2)
if [ -n "$mean" ]; then
    # Digital silence reports about -91 dB, so anything below -80 means the
    # audio track exists but carries nothing.
    if awk -v m="$mean" 'BEGIN{exit !(m+0 < -80)}'; then
        fail "audio track is silent (${mean} dB)"
    fi
    pass "audio carries signal (${mean} dB mean)"
fi

say "Stream state"
curl -sf "$API/stream/$STREAM_ID"; echo

printf '\n\033[32mAll checks passed.\033[0m\n'
