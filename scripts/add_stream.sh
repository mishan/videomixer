#!/usr/bin/env bash
# Add a source to a running stream.
#
#   add_stream.sh <host> <stream_id> <source_id> <source_uri> [x] [y] [z]
#
# Position is optional: under a layout it is overwritten anyway.

INSTANCE=$1
STREAM_ID=$2
SOURCE_ID=$3
SOURCE_URI=$4
XPOS=${5:-0}
YPOS=${6:-0}
ZPOS=${7:-1}

curl -H "Content-Type: application/json" -X PUT \
  -d "{\"stream_uri\":\"${SOURCE_URI}\", \"x\":${XPOS}, \"y\":${YPOS}, \"z\":${ZPOS}}" \
  "http://${INSTANCE}:8888/stream/${STREAM_ID}/${SOURCE_ID}"
