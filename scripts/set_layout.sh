#!/usr/bin/env bash
# Set the layout of a running stream.
#
#   set_layout.sh <host> <stream_id> '<layout json>'
#
# e.g. set_layout.sh localhost asdf '{"preset":"grid"}'
#      set_layout.sh localhost asdf '{"preset":"pip","source":"cam1"}'

INSTANCE=$1
STREAM_ID=$2
LAYOUT=$3

curl -H "Content-Type: application/json" -X PUT -d "${LAYOUT}" \
  "http://${INSTANCE}:8888/stream/${STREAM_ID}/layout"
