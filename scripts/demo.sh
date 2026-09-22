#!/usr/bin/env bash
#
# Record the README demo: four sources join a stream one at a time, the
# stream steps through its layout presets with transitions on, and one
# source leaves. The mixed output is saved as an MP4 and converted to the
# GIF the README embeds.
#
# Assumes the compose stack is up:
#   docker compose up --build -d
#   ./scripts/demo.sh            # writes docs/demo.mp4 and docs/demo.gif
#
# The sources are flat color cards at three aspect ratios, so the fit and
# placement of each cell is easy to read. The storyboard waits for each
# source to connect before holding, so a slow connect stretches nothing but
# the wait -- every shot holds for the same time on every run.
set -euo pipefail

API="${API:-http://localhost:8888}"
RTMP_HOST_URL="${RTMP_HOST_URL:-rtmp://localhost:1935/live}"
RTMP_NET_URL="${RTMP_NET_URL:-rtmp://rtmp:1935/live}"
STREAM_ID="${STREAM_ID:-demo}"
OUT_DIR="${OUT_DIR:-docs}"
# The GIF is scaled down from the 1280x720 mix; the MP4 keeps full size.
GIF_WIDTH="${GIF_WIDTH:-640}"
GIF_FPS="${GIF_FPS:-15}"
# How long to let the publishers come up before the stream pulls them.
SETTLE_PUBLISH="${SETTLE_PUBLISH:-4}"
# The recording trails the API by the encoder and RTMP latency, so it keeps
# running this long after the last step to catch the end of it.
RECORD_TAIL="${RECORD_TAIL:-5}"
TRANSITION='{"duration": 0.6, "easing": "ease-in-out"}'
WORKDIR="$(mktemp -d)"
PIDS=()
RECORDER=
STARTED=

cleanup() {
    local status=$?
    [ -n "$RECORDER" ] && kill -INT "$RECORDER" 2>/dev/null || true
    for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
    curl -sf -X DELETE "$API/stream/$STREAM_ID" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
    exit $status
}
trap cleanup EXIT

need() { command -v "$1" >/dev/null || { echo "error: $1 is required" >&2; exit 1; }; }
need ffmpeg
need curl
need python3   # parses the API's JSON while waiting on sources

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

publish() {  # publish <name> <size> <color> <label>
    ffmpeg -nostdin -loglevel error -re \
        -f lavfi -i "anullsrc=r=44100:cl=stereo" \
        -f lavfi -i "color=c=$3:size=$2:rate=30" \
        -vf "drawtext=text='$4':font=Sans:fontsize=64:fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=14:x=(w-tw)/2:y=(h-th)/2,format=yuv420p" \
        -c:v libx264 -preset ultrafast -tune zerolatency -b:v 500k \
        -c:a aac -b:a 64k \
        -x264opts keyint=30:min-keyint=30:scenecut=-1 \
        -f flv "$RTMP_HOST_URL/$1" >"$WORKDIR/$1.log" 2>&1 &
    PIDS+=($!)
}

put() {  # put <path> <json>
    curl -sf -H 'Content-Type: application/json' -X PUT -d "$2" \
        "$API/stream/$STREAM_ID$1" >/dev/null || fail "PUT $1 failed: $2"
}

layout() {  # layout <json members, without braces> <hold seconds>
    echo "  layout {$1}"
    put /layout "{$1, \"transition\": $TRANSITION}"
    sleep "$2"
}

add() {  # add <source> <hold seconds>
    echo "  add $1"
    put "/$1" "{\"stream_uri\": \"$RTMP_NET_URL/$1\"}"
    wait_connected "$1"
    [ -n "$STARTED" ] || STARTED=$(date +%s.%N)
    sleep "$2"
}

remove() {  # remove <source> <hold seconds>
    echo "  remove $1"
    curl -sf -X DELETE "$API/stream/$STREAM_ID/$1" >/dev/null \
        || fail "DELETE $1 failed"
    sleep "$2"
}

wait_connected() {  # wait_connected <source>
    local i
    for i in $(seq 100); do
        if curl -sf "$API/stream/$STREAM_ID" | python3 -c '
import json, sys
src = json.load(sys.stdin)["mixer"]["sources"].get(sys.argv[1], {})
sys.exit(src.get("connection", {}).get("state") != "connected")' "$1"
        then
            return
        fi
        sleep 0.1
    done
    fail "$1 did not connect within 10s"
}

say "Waiting for the mixer API"
for _ in $(seq 30); do
    curl -sf "$API/health" >/dev/null && break
    sleep 1
done
curl -sf "$API/health" >/dev/null || fail "mixer API is not up at $API -- run 'docker compose up --build -d'"
curl -sf -X DELETE "$API/stream/$STREAM_ID" >/dev/null 2>&1 || true

say "Publishing the sources"
publish one   1280x720 0xDC143C 'ONE 16x9'
publish two   960x720  0x006400 'TWO 4x3'
publish three 720x720  0x00008B 'THREE 1x1'
publish four  1280x720 0xFF8C00 'FOUR 16x9'
sleep "$SETTLE_PUBLISH"
for pid in "${PIDS[@]}"; do
    kill -0 "$pid" 2>/dev/null || fail "a publisher exited early: $(cat "$WORKDIR"/*.log)"
done

say "Creating the stream"
# Empty to begin with, so the first source fades in over black rather than
# the recording opening on a picture already in place.
put "" "{\"output_uri\": \"$RTMP_NET_URL/$STREAM_ID\",
         \"width\": 1280, \"height\": 720, \"fps\": 30,
         \"layout\": {\"preset\": \"grid\", \"transition\": $TRANSITION}}"
sleep 2

say "Recording"
# Stopped with SIGINT rather than -t, which is unreliable against a live
# RTMP input; see test_e2e.sh. Flushing every packet lets the wait below see
# the file start, instead of it sitting in ffmpeg's write buffer.
ffmpeg -nostdin -loglevel error -i "$RTMP_HOST_URL/$STREAM_ID" -c copy -flush_packets 1 \
    -y "$WORKDIR/raw.flv" >"$WORKDIR/record.log" 2>&1 &
RECORDER=$!
# ffmpeg probes the input for a few seconds before it writes anything.
for _ in $(seq 150); do
    [ -s "$WORKDIR/raw.flv" ] && break
    kill -0 "$RECORDER" 2>/dev/null || break
    sleep 0.1
done
[ -s "$WORKDIR/raw.flv" ] || fail "recording did not start: $(cat "$WORKDIR/record.log")"
sleep 0.5

say "Running the storyboard"
add one   1.5
add two   1.5
add three 1.5
add four  2
layout '"preset": "row"' 2
layout '"preset": "spotlight", "source": "two"' 2.5
layout '"preset": "pip", "source": "one"' 2.5
layout '"preset": "solo", "source": "three"' 2
layout '"preset": "grid"' 2
remove four 2.5
# Wall-clock length of the storyboard, from the first source appearing.
length=$(python3 -c 'import sys, time; print(time.time() - float(sys.argv[1]))' "$STARTED")

say "Stopping the recording"
sleep "$RECORD_TAIL"
kill -INT "$RECORDER"
wait "$RECORDER" || true
RECORDER=
[ -s "$WORKDIR/raw.flv" ] || fail "recording is empty: $(cat "$WORKDIR/record.log")"

say "Encoding"
mkdir -p "$OUT_DIR"
# Everything before the first source fades in is black; start just before it
# and run for as long as the storyboard did.
start=$(ffmpeg -nostdin -v info -i "$WORKDIR/raw.flv" \
        -vf blackdetect=d=0.1:pix_th=0.05 -an -f null /dev/null 2>&1 \
        | grep -oE 'black_start:0(\.0+)? black_end:[0-9.]+' | head -1 \
        | sed 's/.*black_end://' || true)
start=$(python3 -c 'import sys; print(max(0.0, float(sys.argv[1] or 0) - 0.3))' "$start")
printf '  keeping %.1fs from %.1fs in\n' "$length" "$start"

ffmpeg -nostdin -loglevel error -ss "$start" -t "$length" \
    -i "$WORKDIR/raw.flv" -an \
    -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p -movflags +faststart \
    -y "$OUT_DIR/demo.mp4"

# A palette built from the video itself keeps the card colors exact, and
# diff_mode=rectangle only re-encodes the region that changed between frames,
# which is most of what keeps a GIF of mostly-static cards small.
ffmpeg -nostdin -loglevel error -i "$OUT_DIR/demo.mp4" -filter_complex \
    "fps=$GIF_FPS,scale=$GIF_WIDTH:-1:flags=lanczos,split[a][b];
     [a]palettegen=max_colors=64:stats_mode=diff[p];
     [b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
    -y "$OUT_DIR/demo.gif"

printf '\n\033[32mWrote %s (%s bytes) and %s (%s bytes).\033[0m\n' \
    "$OUT_DIR/demo.mp4" "$(stat -c%s "$OUT_DIR/demo.mp4")" \
    "$OUT_DIR/demo.gif" "$(stat -c%s "$OUT_DIR/demo.gif")"
