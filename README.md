videomixer
==========

videomixer is video streaming middleware built on GStreamer. It composites any
number of live RTMP inputs into a single H.264/AAC stream and pushes the result
to an RTMP destination, with an HTTP API for adding, moving, resizing and
removing picture-in-picture layers on a running stream.

It started as a hackathon proof of concept. Video mixing worked; audio did not.


Quick start
-----------

    docker compose up --build -d
    ./scripts/test_e2e.sh

That brings up an nginx-rtmp server and the mixer, publishes two test patterns,
mixes them, pulls the result back and asserts it contains both video and audio.

The mixer's API is on `localhost:8888`, RTMP on `localhost:1935`, and
nginx-rtmp's stat page on `localhost:8890/stat`.


API
---

All endpoints take and return JSON. Errors come back as
`{"status": "FAIL", "error": "..."}` with a meaningful HTTP status.

### `PUT /stream/{stream_id}`

Create a mixed output stream.

| field           | required | default | meaning                          |
|-----------------|----------|---------|----------------------------------|
| `output_uri`    | yes      |         | RTMP destination                 |
| `bg_uri`        | no       |         | RTMP source for the background   |
| `width`         | no       | 1280    | output width                     |
| `height`        | no       | 720     | output height                    |
| `fps`           | no       | 30      | output frame rate                |
| `video_bitrate` | no       | 2500    | kbps                             |
| `audio_bitrate` | no       | 128     | kbps                             |

`bg_uri` is optional: a stream can start empty and have sources added later.

    curl -H "Content-Type: application/json" -X PUT \
      -d '{"bg_uri":"rtmp://rtmp:1935/live/testpattern",
           "output_uri":"rtmp://rtmp:1935/live/mixed"}' \
      http://localhost:8888/stream/asdf

### `PUT /stream/{stream_id}/{pip_id}`

Add a picture-in-picture layer to a stream. Works on a running pipeline; the
output does not restart.

| field        | required | default | meaning                            |
|--------------|----------|---------|------------------------------------|
| `stream_uri` | yes      |         | RTMP source to overlay             |
| `x`, `y`     | no       | 0       | position of the top-left corner    |
| `z`          | no       | 1       | z-order; the background is 0       |
| `width`      | no       | native  | scale to this width                |
| `height`     | no       | native  | scale to this height               |

    curl -H "Content-Type: application/json" -X PUT \
      -d '{"stream_uri":"rtmp://rtmp:1935/live/cam",
           "x":20, "y":20, "z":10, "width":320, "height":180}' \
      http://localhost:8888/stream/asdf/pipstream1

### The rest

| method   | path                                   | does                              |
|----------|----------------------------------------|-----------------------------------|
| `GET`    | `/health`                              | liveness, plus a stream count     |
| `GET`    | `/streams`                             | list stream ids                   |
| `GET`    | `/stream/{id}`                         | pipeline state and layer geometry |
| `DELETE` | `/stream/{id}`                         | tear the stream down              |
| `DELETE` | `/stream/{id}/{pip_id}`                | remove one layer                  |
| `POST`   | `/stream/{id}/move/{pip_id}`           | change `x`, `y`, `z`              |
| `POST`   | `/stream/{id}/resize/{pip_id}`         | change `width`, `height`          |


How it works
------------

Each stream is one GStreamer pipeline:

    videotestsrc(black) -.
    RTMP source video ---+-> compositor -> videoconvert -> queue -> x264enc
                                                                      |
                                                                  h264parse
                                                                      |
                                                                    queue
                                                                      v
                                                                   flvmux -> rtmp2sink
                                                                      ^
                                                                    queue
                                                                      |
                                                                  aacparse
                                                                      |
    audiotestsrc(silence) -.                                      avenc_aac
    RTMP source audio -----+-> audiomixer -> queue -> convert -> resample

The black video layer and the silent audio layer are load-bearing, not
decoration. See below.


Why audio used to freeze
------------------------

`flvmux` is an aggregator: it emits nothing until *every* sink pad has data,
and a pad that never receives a buffer blocks the entire pipeline — including
video, which is why the symptom looked like the video mixer hanging.

Two things fed that:

* The audio branch terminated at `avenc_aac` with an unlinked src pad. An
  unlinked pad returns `GST_FLOW_NOT_LINKED`, which propagates upstream and
  stops the stream with an opaque "Internal data stream error". Linking it to
  `flvmux` then gave the muxer an audio pad it had to wait on, and real RTMP
  sources connect late, drop out, and often carry no audio track at all.
* `rtmpsrc` — the old librtmp element — fails outright against current RTMP
  servers, with the same unhelpful error.

So: `rtmp2src` replaces `rtmpsrc`, every demuxer pad is consumed (unused ones
get a `fakesink` rather than being ignored), and the black/silent base layers
guarantee the muxer is never starved on either branch. Elements added to a
running pipeline also get `sync_state_with_parent()`, without which a newly
added PiP sits in NULL state and silently produces nothing.

### Late-joining sources have to be offset into the running time

Because the base layers are live, `compositor` and `audiomixer` run as live
aggregators and emit on a timer. An RTMP source needs roughly a second to
connect, demux and decode, by which point the aggregator has already advanced.
The newly linked pad then delivers buffers timestamped from the start of *its*
stream, which lands in the aggregator's past and is discarded.

The symptom is a source that decodes with no error anywhere in the log yet
contributes nothing — silent audio, or a layer that never appears. Whether it
happened at all came down to timing, so it looked intermittent: the same build
would mix audio correctly on one run and emit an AAC track containing zero
packets on the next.

`RtmpSource._align_to_running_time` fixes it by offsetting each mixer sink pad
by the running time at which the source joined, mapping its stream start onto
now. The mixers also carry `min-upstream-latency` and `ignore-inactive-pads`,
which reserve headroom for sources plugged in after playback started and stop
a dropped publisher from stalling the mix.

If the mix ever goes quiet again while the logs look clean, this is the first
thing to check. `scripts/test_e2e.sh` asserts on mean volume specifically to
catch it, and fails rather than skipping if it cannot measure a level at all.


Other things that changed
-------------------------

* The HTTP handlers were `def` functions using `yield from`, which was
  aiohttp 2.x style. On aiohttp 3.x they return generator objects.
  So now they are ordinary coroutines.
* `gbulb` and `asyncio_glib` are gone. The GLib main loop runs on its own
  daemon thread and aiohttp owns the main thread; GStreamer is thread-safe, so
  the two loops never needed fusing.
* `videomixer` -> `compositor`, `rtmpsink` -> `rtmp2sink`.
* There is a bus watch now, so pipeline errors get logged instead of vanishing.
* `resize` and `move` called methods that did not exist, and `move` referenced
  undefined variables. Both work.
* `DELETE` endpoints are implemented, and tear pipelines down properly.
* The `videomixer-base` image is gone; the apt layer caches on its own, and
  needing to build a base image by hand first was a footgun.
* Base images are pinned to Debian trixie. nginx-rtmp was on Debian jessie,
  which has been EOL since 2020 and whose repos have moved to archive.


Development
-----------

`make` on its own lists the available targets:

| target | does                                        |
|--------|---------------------------------------------|
| `dev`  | install dev dependencies (flake8)           |
| `lint` | run flake8, configured in `setup.cfg`       |
| `test` | bring the stack up and run the e2e check    |
| `up`   | build and start the compose stack           |
| `down` | stop the stack and remove volumes           |
| `logs` | follow the container logs                   |

Dev dependencies live in `requirements-dev.txt`, not `requirements.txt` — the
latter is what the Dockerfile installs, and a linter has no business in the
runtime image.

    pip install -r requirements-dev.txt
    make check

### Tests

`tests/` runs in well under a second and needs no Docker, no network and no
RTMP server. `rtmp2sink` only connects on the transition out of NULL, so a
pipeline can be fully constructed and asserted on without anything opening a
socket.

They deliberately use real GStreamer rather than mocking it. Every serious bug
this project had — an encoder silently rejecting caps, an audio branch that
never reached the muxer, a base layer occluding the background — lived in the
wiring, and a mock of `Gst` would have accepted all of them. `test_pipeline.py`
asserts those invariants directly; `test_api.py` covers routing, validation and
status codes against a fake mixer, including that every handler is a coroutine.

### Regenerating the diagrams

`docs/example_pipeline.png` is a Graphviz dump of a real running pipeline, not
a hand-drawn picture. Set `GST_DEBUG_DUMP_DOT_DIR` and the mixer writes one on
every pipeline change (`VideoMixer.dump_dot`); unlike `gst-launch`, an
application has to ask for these explicitly.

    GST_DEBUG_DUMP_DOT_DIR=/tmp/dot python3 mix.py
    # create a stream and add a PiP, then:
    dot -Tpng /tmp/dot/videomixer-playing.dot -o docs/example_pipeline.png

`docs/videomix.uml` is the hand-maintained overview of the same graph with the
queues elided:

    plantuml docs/videomix.uml


Running without Docker
----------------------

You need GStreamer 1.20+ with the base, good, bad, ugly and libav plugin sets,
plus `python3-gi` from your distro (not pip — it has to match the system
typelibs).

    pip install -r requirements.txt
    python3 mix.py --port 8888

    --bind        address to bind (env `MIX_BIND`)
    --port        API port (env `MIX_PORT`)
    --log-level   DEBUG/INFO/WARNING/ERROR (env `MIX_LOG_LEVEL`)
    --gst-debug   GStreamer debug threshold, 0 disables (env `GST_DEBUG_LEVEL`)


Known gaps
----------

* A source that disconnects is not automatically reconnected; the layer goes
  black and the stream keeps running.
* Audio focus, ducking and normalization are not implemented — every source is
  mixed at unity gain.
* There are no unit tests, only the end-to-end script.
