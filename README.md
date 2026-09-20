videomixer
==========

videomixer is video streaming middleware built on GStreamer. It composites any
number of live RTMP inputs into a single H.264/AAC stream and pushes the result
to an RTMP destination, with an HTTP API for adding and removing sources and
arranging them on a running stream — side by side, stacked, a grid, one large
with the rest inset, or any set of rectangles the operator specifies.

It never got past proof of concept: video mixing worked, audio did not.


Origins
-------

This project began in 2018 as a hackathon project at
[The Meet Group](https://www.themeetgroup.com/), who open sourced it as
`themeetgroup/videomixer`. It has since been detached from that repository and
continues here.

With thanks to The Meet Group for open sourcing it originally. The 2018 work
remains copyright The Meet Group Inc and the project is still MIT licensed —
see [LICENSE](LICENSE).


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
| `layout`        | no       |         | layout to start with — see below |

`bg_uri` is optional: a stream can start empty and have sources added later.

    curl -H "Content-Type: application/json" -X PUT \
      -d '{"bg_uri":"rtmp://rtmp:1935/live/testpattern",
           "output_uri":"rtmp://rtmp:1935/live/mixed"}' \
      http://localhost:8888/stream/asdf

`layout` is optional and takes the same body as `PUT /stream/{id}/layout`
below. Setting it here means the background lands in its cell rather than
going out full-frame and then jumping.

### `PUT /stream/{stream_id}/{source_id}`

Add a source to a stream. Works on a running pipeline; the output does not
restart.

| field        | required | default   | meaning                            |
|--------------|----------|-----------|------------------------------------|
| `stream_uri` | yes      |           | RTMP source to mix in              |
| `x`, `y`     | no       | 0         | position of the top-left corner    |
| `z`          | no       | 1         | z-order; the background is 0       |
| `width`      | no       | native    | scale to this width                |
| `height`     | no       | native    | scale to this height               |
| `fit`        | no       | `contain` | `contain` or `fill` — see below    |
| `alpha`      | no       | 1.0       | opacity, 0 to 1                    |

    curl -H "Content-Type: application/json" -X PUT \
      -d '{"stream_uri":"rtmp://rtmp:1935/live/cam",
           "x":20, "y":20, "z":10, "width":320, "height":180}' \
      http://localhost:8888/stream/asdf/cam1

Under a layout the geometry here is only a starting point: the layout is
re-resolved with the new source included and overwrites it.

`layout` is a reserved source id: `/stream/{id}/layout` is the layout
endpoint, so it cannot also name a source. Trying to add one comes back as a
409 saying so.

### The rest

| method   | path                                   | does                              |
|----------|----------------------------------------|-----------------------------------|
| `GET`    | `/health`                              | liveness, plus a stream count     |
| `GET`    | `/streams`                             | list stream ids                   |
| `GET`    | `/stream/{id}`                         | pipeline state and layer geometry |
| `DELETE` | `/stream/{id}`                         | tear the stream down              |
| `DELETE` | `/stream/{id}/{source_id}`             | remove one source                 |
| `POST`   | `/stream/{id}/move/{source_id}`        | change `x`, `y`, `z`              |
| `POST`   | `/stream/{id}/resize/{source_id}`      | change `width`, `height`          |
| `POST`   | `/stream/{id}/fit/{source_id}`         | change `fit`                      |
| `POST`   | `/stream/{id}/alpha/{source_id}`       | change `alpha`                    |


Layout
------

A stream's layout is one JSON object describing where its sources go. It is
stored as written and *re-resolved every time the set of connected sources
changes*, which is what lets a grid reshape itself as publishers join and drop
— the operator says "grid", not "grid of these four, and now these five".

### `PUT /stream/{stream_id}/layout`

    curl -H "Content-Type: application/json" -X PUT \
      -d '{"preset":"grid"}' \
      http://localhost:8888/stream/asdf/layout

The response carries the resolved rectangles, so a control surface can draw
what the mixer is actually doing without recomputing it:

    {"status": "OK",
     "layout": {"preset": "grid"},
     "cells": {"cam1": {"x": 0, "y": 0, "width": 640, "height": 360,
                        "z": 1, "fit": "contain", "alpha": 1.0},
               "cam2": {"x": 640, "y": 0, ...}}}

`GET` returns the same thing. `DELETE` stops tracking the layout without
moving anything: the picture stays exactly as it is, but the next source to
join or drop no longer reshapes it.

### Transitions

Add `transition` to a preset or cells layout to animate changes to position,
size, and opacity. It stays in the stored layout spec, so sources joining or
leaving also animate the remaining cells:

    {"preset": "grid", "transition": {"duration": 0.4, "easing": "ease-in-out"}}

Duration is in seconds, greater than zero and at most 2. The default is 0.4.
Easing may be `linear`, `ease-in`, `ease-out`, or `ease-in-out` (the default).
Without `transition`, layout changes remain immediate. A newly joined source
fades in at its destination; removal is immediate. Moving, resizing, changing
fit, or changing opacity by hand cancels that source's animation and clears
the tracked layout.

### Presets

| preset                  | does                                                |
|-------------------------|-----------------------------------------------------|
| `grid`                  | a near-square grid, shaped to the number of sources |
| `row`                   | side by side                                        |
| `column`                | one above the other                                 |
| `solo`                  | one source full-frame, the rest hidden              |
| `pip`                   | one source full-frame, the rest inset over it       |
| `spotlight`             | one source large, the rest in a strip beside it     |

`side-by-side`, `stacked`, `horizontal`, `vertical` and `fullscreen` are
accepted as aliases.

Every preset takes `gap` and `margin` (pixels between cells, and around the
canvas), `fit`, `order` and `exclude`:

    {"preset": "grid", "gap": 8, "margin": 16, "order": ["host", "guest"]}
    {"preset": "grid", "exclude": ["backstage"]}

`order` pins the leading positions and everything else follows in the order it
was added. Naming a source that has not connected yet is fine — it takes its
place when it arrives.

`grid` also takes `rows` and `cols`, and `last_row`:

    {"preset": "grid", "cols": 3}                  # 3 across, rows as needed
    {"preset": "grid", "cols": 3, "last_row": "justify"}

`rows` and `cols` are floors, not caps. A grid asked for three columns and
handed seven sources grows a third row rather than dropping anyone: nothing an
operator does to the layout should make a live publisher vanish.

A final row short of a full one is centred under the rows above by default,
because that reads as intentional — `"last_row": "justify"` stretches it
across instead, which makes those cells wider than every other cell in the
grid.

`solo`, `pip` and `spotlight` take `source` — the one they are built around,
defaulting to the first. `pip` also takes `size` (the inset's fraction of the
canvas, default 0.25) and `corner`; `spotlight` takes `size` (the strip's
share) and `position` (`bottom`, `top`, `left` or `right`).

    {"preset": "pip", "source": "host", "size": 0.3, "corner": "top-right"}
    {"preset": "spotlight", "source": "speaker", "position": "left"}

`pip` insets march inward from the chosen corner, and shrink below `size` once
there are more of them than fit along that edge — for the same reason a grid
grows a row rather than truncating. Nothing an operator does to the layout
should push a live publisher off the canvas.

A source `solo` has no room for is dropped to `alpha` 0 rather than torn down.
It keeps its branch and its mixer pad, so bringing it back is a property change
and not a reconnect.

### Cells

For the layouts no preset covers, place the rectangles yourself:

    {"cells": [{"source": "cam1", "x": 0,   "y": 0,   "width": 640, "height": 720},
               {"source": "cam2", "x": 640, "y": 0,   "width": 640, "height": 360},
               {"source": "cam3", "x": 640, "y": 360, "width": 640, "height": 360}]}

| field            | required | default   | meaning                          |
|------------------|----------|-----------|----------------------------------|
| `source`         | yes      |           | which source this cell places    |
| `width`,`height` | yes      |           | size of the cell                 |
| `x`, `y`         | no       | 0         | top-left corner                  |
| `z`              | no       | 1         | z-order; the background is 0     |
| `fit`            | no       | layout's  | `contain` or `fill`              |
| `alpha`          | no       | 1.0       | opacity, 0 to 1                  |

Cells differ from a preset in one way that matters: a preset gives every
connected source a cell, while cells only ever touch the sources they name. A
hand-placed overlay survives a cells layout that does not mention it.

A cell may name a source that has not connected yet, so an operator can
describe the whole show up front and have each camera land in its slot as it
comes up.

### Fractions

`"units": "fraction"` reads `x`, `y`, `width` and `height` as fractions of the
canvas instead of pixels, so the same layout survives a change of output
resolution:

    {"units": "fraction",
     "cells": [{"source": "cam1", "x": 0,   "y": 0,    "width": 0.5, "height": 1.0},
               {"source": "cam2", "x": 0.5, "y": 0.25, "width": 0.5, "height": 0.5}]}

`gap` and `margin` stay in pixels either way — a gap that scales with the
canvas is not what anyone means by one.

### Fit

Sources rarely match the aspect ratio of the cell they are put in. `contain`,
the default, scales the source to fit and leaves the rest of the cell alone, so
whatever is underneath shows through: black from the mixer's base layer in a
grid, the background source under a PiP inset. `fill` stretches the source to
the cell, which is what the mixer used to do unconditionally.

This is the compositor's own `sizing-policy`, which arrived in GStreamer 1.20.
On anything older every source is stretched and a warning in the log says so.

### Layouts and moving things by hand

`move`, `resize`, `fit` and `alpha` all set something a layout's cells carry,
so under a live layout their effect would last only until the next publisher
joined and the layout put it back. Rather than silently undo the operator, or
refuse the request, the manual placement wins and the layout stops being
tracked — the same state `DELETE /stream/{id}/layout` leaves behind.

A layout that stops resolving is dropped rather than raised: removing the
subject of a `solo` succeeds, the remaining sources stay where they are, and
the log says why the layout is no longer in force. An operator's request to
remove a source should never fail because of how the stream happens to be
arranged.


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
added source sits in NULL state and silently produces nothing.

Reconnection
------------

Sources reconnect on their own. If a publisher drops, the layer goes black, the
output keeps running, and the source retries at roughly 1s, 2s, 4s, 8s and then
every 10s indefinitely until it comes back. Geometry survives, so the layer
returns exactly where it was. This also means a source can be added before its
camera is live — it simply retries until the stream appears.

Each delay is jittered across the back half of its interval (so the "1s" retry
actually falls between 0.5s and 1s). One outage usually takes every source with
it, and without jitter they would all come back on an identical schedule and
retry in lockstep forever, hitting the server in bursts.

The cap is deliberately low, because it is also the worst case for how long a
layer stays black once its source is reachable again. At 30s, a source cycling
faster than the current delay had every retry land in one of its gaps: four
consecutive 4s-up/3s-down cycles produced no recovery at all. At 10s the same
pattern recovers on each cycle.

`GET /stream/{id}` reports the state of each source:

    "connection": {
      "state": "reconnecting",
      "reconnect_attempts": 3,
      "last_error": "Failed to connect: 'play' cmd failed: ..."
    }

### Why a dropped publisher needs a pad probe to notice

A source going away is invisible at the pipeline level. `rtmp2src` emits EOS on
its src pad, but an aggregator only forwards EOS once *every* pad has ended, and
the base layers never end — so nothing reaches the bus, nothing is logged, and
the layer just turns black and stays that way. A pad probe on `rtmp2src` catches
that EOS and drives the rebuild; the event is dropped rather than forwarded, so
it never marks the mixer's own sink pads finished.

Two other paths lead to the same handler. Errors from a source's elements are
attributed back to the owning source and reconnect it rather than being logged
and ignored. And `rtmp2src` carries an `idle-timeout`, because a half-open TCP
connection produces neither EOS nor an error — without it a source can sit black
forever with nothing to detect.

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
* Sources are called sources. They were "PiPs" throughout — `pip_id` in every
  signature and URL template, `pip_streams` in the status body — from when a
  picture-in-picture overlay was the only thing the mixer could do. It is one
  layout preset now, so the name was wrong everywhere else. The path templates
  were internal (`/stream/asdf/cam1` is unchanged), but two wire-visible keys
  moved: `pip_id` is now `source_id` in responses, and `pip_streams` is now
  `sources` in `GET /stream/{id}`.
* Sources default to `contain` rather than being stretched. A source given an
  explicit `width` and `height` that do not match its aspect ratio used to be
  distorted with no way to ask for anything else.


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

### Your virtualenv has to see the system PyGObject

`gi` comes from your distro, not pip, because it has to match the system
GObject introspection typelibs. A virtualenv hides system packages unless
told otherwise, so a plain `python3 -m venv` leaves `import gi` failing and
nothing will run — not even the API tests, since `mixerapi` imports
`videomixer` which imports `gi`.

    make venv          # python3 -m venv --system-site-packages .venv
    . .venv/bin/activate
    make check

For an existing venv, recreate it with `--system-site-packages`; the flag
cannot be added afterwards. If you would rather not set GStreamer up on the
host at all, `make unit-docker` runs the suite inside the container.

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

`test_layout.py` is the exception, and deliberately so: `layout.py` is pure
arithmetic and imports neither `gi` nor anything that does, so those tests run
anywhere Python does. The invariants worth holding onto there are that cells
tile their canvas exactly — a one-pixel seam down the right-hand column of a
3-up is visible on air — and that no layout can make a connected source
disappear by accident.

### Regenerating the diagrams

`docs/example_pipeline.png` is a Graphviz dump of a real running pipeline, not
a hand-drawn picture. Set `GST_DEBUG_DUMP_DOT_DIR` and the mixer writes one on
every pipeline change (`VideoMixer.dump_dot`); unlike `gst-launch`, an
application has to ask for these explicitly.

    GST_DEBUG_DUMP_DOT_DIR=/tmp/dot python3 mix.py
    # create a stream and add a source, then:
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

* Audio focus, ducking and normalization are not implemented — every source is
  mixed at unity gain.
* Recovery latency is bounded by the backoff cap: once a source is reachable
  again it can take up to `RECONNECT_MAX_DELAY` (10s) to be picked up. A source
  flapping faster than that can still miss a window, though it recovers on the
  following cycle. Lowering the cap further trades connection load for recovery
  time.
* `fit` has no `cover`. Filling a cell edge to edge with no bars and no
  distortion means cropping the overflow, which `compositor` cannot do on its
  own — it needs a `videocrop` per source, recomputed whenever a cell resizes
  or the source renegotiates its size.
* Layout changes are instant. There is no transition between them, so a switch
  from a grid to a solo is a hard cut on the frame it lands.
* A layout cannot reference a source by anything but its id, so there is no
  "whoever is speaking" or "most recent to join" without a control surface
  computing it and setting a new layout.
