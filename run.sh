#!/usr/bin/env bash
# Bring up the RTMP server and the mixer together.
set -xue

docker compose up --build -d
docker compose ps
