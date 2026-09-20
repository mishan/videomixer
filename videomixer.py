#!/usr/bin/env python3
"""Compositing RTMP mixer built on GStreamer.

A VideoMixer owns one GStreamer pipeline that composites any number of RTMP
sources into a single H.264/AAC FLV stream and pushes it to an RTMP sink.

Pipeline shape::

    videotestsrc(black) -.
    RtmpSource video ----+-> compositor -> videoconvert -> queue -> x264enc
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
    RtmpSource audio ------+-> audiomixer -> queue -> convert -> resample

The two synthetic base layers (black video, silent audio) are what keep this
from deadlocking. flvmux is an aggregator: it will not emit a single byte until
every one of its sink pads has data, and an aggregator pad that never receives
a buffer blocks the whole pipeline. Real RTMP sources connect late, disconnect,
and frequently carry no audio track at all, so without a layer that is always
live the muxer starves and the stream freezes -- which is exactly the failure
this project had before.
"""

import logging
import os

import layout
import rtmpsource
import transition

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib  # noqa: E402

log = logging.getLogger(__name__)

# flvmux only supports a fixed set of audio rates; 44.1kHz stereo is the
# safe interoperable choice for RTMP.
AUDIO_CAPS = 'audio/x-raw,rate=44100,channels=2,format=S16LE,layout=interleaved'

# avenc_aac accepts F32LE and nothing else, so the caps handed to the encoder
# differ from the caps used on the mixer's inputs.
ENCODER_AUDIO_CAPS = ('audio/x-raw,rate=44100,channels=2,format=F32LE,'
                      'layout=interleaved')

# Headroom the compositor and audiomixer allow for a source to deliver buffers
# for the position they are currently aggregating. RTMP sources connect,
# demux and decode over roughly a second, so they need room.
AGGREGATOR_LATENCY = 1 * Gst.SECOND


class VideoMixer:
    """One RTMP output stream, composited from many RTMP inputs."""

    def __init__(self, output_url, width=1280, height=720, fps=30,
                 video_bitrate=2500, audio_bitrate=128):
        self.sources = {}
        # Sources removed from layout membership but still fading on air.
        self._retiring_sources = {}
        # The layout spec currently in force, or None while geometry is being
        # driven one source at a time through move/resize. It is kept as the
        # spec rather than as resolved rectangles because it has to be
        # re-resolved every time the set of connected sources changes -- that
        # is what makes a grid reshape itself when a publisher joins or drops.
        self.layout = None
        self.output_url = output_url
        self.width = width
        self.height = height
        self.fps = fps
        self.video_bitrate = video_bitrate
        self.audio_bitrate = audio_bitrate
        self.initialize()

    # -- lifecycle ---------------------------------------------------------

    def dump_dot(self, suffix):
        """Write a Graphviz dump of the pipeline, if dumping is enabled.

        Set GST_DEBUG_DUMP_DOT_DIR to a directory and every pipeline change
        lands there as a .dot file; render one with::

            dot -Tpng pipeline.dot -o pipeline.png

        Unlike gst-launch, an application has to ask for these explicitly, so
        this is called at the points worth inspecting. It is a no-op when the
        environment variable is unset.
        """
        if not os.environ.get('GST_DEBUG_DUMP_DOT_DIR'):
            return
        name = 'videomixer-{}'.format(suffix)
        Gst.debug_bin_to_dot_file(self.pipeline, Gst.DebugGraphDetails.ALL, name)
        log.debug('Wrote pipeline graph %s.dot', name)

    def play(self):
        log.info('Starting pipeline -> %s', self.output_url)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.dump_dot('playing')

    def pause(self):
        log.info('Pausing pipeline -> %s', self.output_url)
        self.pipeline.set_state(Gst.State.PAUSED)

    def shutdown(self):
        """Tear the pipeline down and release every source."""
        log.info('Shutting down pipeline -> %s', self.output_url)
        for source_id in list(self.sources):
            try:
                self.sources[source_id].remove()
            except Exception:
                log.exception('Error removing source %s', source_id)
        self.sources.clear()
        for source in list(self._retiring_sources.values()):
            try:
                source.remove()
            except Exception:
                log.exception('Error removing fading source %s', source.location)
        self._retiring_sources.clear()
        if self.bus_watch_id is not None:
            GLib.source_remove(self.bus_watch_id)
            self.bus_watch_id = None
        self.pipeline.set_state(Gst.State.NULL)

    # -- sources -----------------------------------------------------------

    def add_rtmp_source(self, source_id, location, xpos=0, ypos=0, zorder=1,
                        width=None, height=None, fit=layout.CONTAIN,
                        alpha=1.0):
        if source_id in self.sources:
            raise ValueError('source_id={} already exists'.format(source_id))
        # An ID becomes available as soon as removal is requested. A quick
        # re-add cuts short the old fade so two pads with that ID cannot overlap.
        retiring = self._retiring_sources.pop(source_id, None)
        if retiring is not None:
            retiring.remove()
        source = rtmpsource.RtmpSource(location, self.pipeline,
                                       self.compositor, self.audiomixer,
                                       xpos, ypos, zorder, width, height,
                                       fit, alpha, fps=self.fps)
        self.sources[source_id] = source
        # Under a layout the geometry passed in is only a starting point: the
        # layout is re-resolved with the new source included and overwrites it.
        self.apply_layout(joining=source_id)
        self.dump_dot('source-{}'.format(source_id))
        return source

    def remove_rtmp_source(self, source_id):
        source = self._get(source_id)
        settings = transition.settings(self.layout) if self.layout is not None else None
        fading = False
        if settings is not None:
            duration, easing = settings
            self._retiring_sources[source_id] = source
            try:
                fading = source.fade_out_then_remove(
                    duration, easing, self.width, self.height,
                    lambda: self._finish_retiring_source(source_id, source))
            finally:
                if not fading:
                    self._retiring_sources.pop(source_id, None)
        if not fading:
            source.remove()
        del self.sources[source_id]
        # The remaining sources close the gap: a 4-up becomes a 3-up.
        self.apply_layout()

    def _finish_retiring_source(self, source_id, source):
        if self._retiring_sources.get(source_id) is not source:
            return
        source.remove()
        self._retiring_sources.pop(source_id, None)

    def resize_rtmp_source(self, source_id, width, height):
        self._get(source_id).resize(width, height)
        self._layout_overridden('resize', source_id)

    def move_rtmp_source(self, source_id, xpos, ypos, zorder):
        self._get(source_id).move(xpos, ypos, zorder)
        self._layout_overridden('move', source_id)

    def set_source_fit(self, source_id, fit):
        self._get(source_id).set_fit(fit)
        self._layout_overridden('fit', source_id)

    def set_source_alpha(self, source_id, alpha):
        self._get(source_id).set_alpha(alpha)
        self._layout_overridden('alpha', source_id)

    def _get(self, source_id):
        if source_id not in self.sources:
            raise KeyError('source_id={} does not exist'.format(source_id))
        return self.sources[source_id]

    # -- layout ------------------------------------------------------------

    def set_layout(self, spec):
        """Adopt a layout spec and place every source it covers.

        Resolution happens before anything is stored, so a spec that does not
        make sense leaves the stream exactly as it was rather than half moved.
        """
        cells = self._resolve(spec)
        self.layout = spec
        self._place(cells, spec)
        log.info('[%s] layout: %s', self.output_url, spec)
        self.dump_dot('layout')
        return cells

    def apply_layout(self, joining=None):
        """Re-resolve the current layout against the sources connected now.

        Called on every membership change. A layout that has stopped resolving
        -- a solo whose subject was just removed, say -- is dropped rather than
        allowed to fail the add or remove that triggered it: the operator's
        request succeeds, the sources keep their geometry, and the log says
        why the layout is no longer in force.
        """
        if self.layout is None:
            return {}
        try:
            cells = self._resolve(self.layout)
        except layout.LayoutError as exc:
            log.warning('[%s] layout %s no longer resolves (%s); leaving '
                        'sources where they are', self.output_url,
                        self.layout, exc)
            self.clear_layout()
            return {}
        self._place(cells, self.layout, joining)
        return cells

    def clear_layout(self):
        """Stop tracking a layout. Sources stay exactly where they are."""
        for source in list(self.sources.values()):
            source.freeze_transition()
        self.layout = None

    def resolved_layout(self):
        """The current layout as rectangles, for reporting."""
        if self.layout is None:
            return {}
        try:
            return self._resolve(self.layout)
        except layout.LayoutError:
            return {}

    def _resolve(self, spec):
        return layout.resolve(spec, self.width, self.height,
                              list(self.sources))

    def _place(self, cells, spec, joining=None):
        # An explicit cells layout may leave sources out. Their last layout
        # must not keep moving them after it has been replaced.
        for source_id, source in list(self.sources.items()):
            if source_id not in cells:
                source.freeze_transition()
        settings = transition.settings(spec)
        if settings is None:
            for source_id, cell in cells.items():
                self.sources[source_id].set_cell(cell)
            return
        duration, easing = settings
        starts = {source_id: (None if source_id == joining else
                              self.sources[source_id].current_transition_cell(
                                  self.width, self.height))
                  for source_id in cells}
        frames = transition.plan(starts, cells, duration, easing)
        for source_id, cell in cells.items():
            self.sources[source_id].start_transition(
                frames[source_id], cell, duration, joining=source_id == joining)

    def _layout_overridden(self, what, source_id):
        """Drop the layout after a source is placed by hand.

        Cells carry fit and alpha as well as a rectangle, so every one of these
        calls is something the layout would set again on its next resolve --
        placing a source by hand under a live layout would otherwise last only
        until the next publisher joined. Rather than silently undo the
        operator, or refuse the request, the manual placement wins and the
        layout stops being tracked.
        """
        if self.layout is None:
            return
        log.info('[%s] %s of %s overrides layout %s; layout cleared',
                 self.output_url, what, source_id, self.layout)
        self.clear_layout()

    def get_info(self):
        return {
            'output_uri': self.output_url,
            'width': self.width,
            'height': self.height,
            'fps': self.fps,
            'state': self.pipeline.get_state(0)[1].value_nick,
            'layout': self.layout,
            'sources': {source_id: source.get_info()
                        for source_id, source in self.sources.items()},
        }

    # -- construction ------------------------------------------------------

    def _make(self, factory, name=None, **props):
        """Create an element, or raise a useful error if the plugin is absent."""
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeError(
                'GStreamer element "{}" is unavailable -- is the matching '
                'gst-plugins package installed?'.format(factory))
        for key, value in props.items():
            element.set_property(key.replace('_', '-'), value)
        self.pipeline.add(element)
        return element

    def initialize(self):
        self.bus_watch_id = None
        self.pipeline = Gst.Pipeline.new()
        if self.pipeline is None:
            raise RuntimeError('Could not create GStreamer pipeline')

        # --- video ---
        # Both mixers are aggregators fed by live base layers, so they run in
        # live mode and emit on a timer. ignore_inactive_pads keeps a source
        # that has stopped delivering (a dropped RTMP publisher) from stalling
        # the mix, and min_upstream_latency reserves headroom for sources that
        # get plugged in after playback has started, which is the normal case
        # here -- every source arrives late.
        self.compositor = self._make('compositor', 'compositor',
                                     background='black',
                                     latency=AGGREGATOR_LATENCY,
                                     min_upstream_latency=AGGREGATOR_LATENCY,
                                     ignore_inactive_pads=True)
        videoconvert = self._make('videoconvert')
        video_caps = self._make('capsfilter', 'outcaps')
        video_caps.set_property('caps', Gst.Caps.from_string(
            'video/x-raw,format=I420,width={},height={},framerate={}/1'.format(
                self.width, self.height, self.fps)))
        video_queue = self._make('queue', 'vqueue',
                                 max_size_time=2 * Gst.SECOND,
                                 leaky=2)
        # zerolatency keeps the encoder from holding frames back, which
        # otherwise shows up as seconds of latency on a live stream.
        self.x264enc = self._make('x264enc',
                                  tune='zerolatency',
                                  speed_preset='veryfast',
                                  bitrate=self.video_bitrate,
                                  key_int_max=self.fps * 2)
        x264_caps = self._make('capsfilter', 'h264caps')
        x264_caps.set_property('caps', Gst.Caps.from_string(
            'video/x-h264,profile=baseline'))
        h264parse = self._make('h264parse')
        video_mux_queue = self._make('queue', 'vmuxqueue')

        # A permanently-live black layer under everything else, so the
        # compositor keeps producing frames even with no sources attached.
        self.video_base = self._make('videotestsrc', 'videobase',
                                     pattern='black', is_live=True)
        base_caps = self._make('capsfilter', 'basecaps')
        base_caps.set_property('caps', Gst.Caps.from_string(
            'video/x-raw,format=I420,width={},height={},framerate={}/1'.format(
                self.width, self.height, self.fps)))

        # --- audio ---
        self.audiomixer = self._make('audiomixer', 'audiomixer',
                                     latency=AGGREGATOR_LATENCY,
                                     min_upstream_latency=AGGREGATOR_LATENCY,
                                     ignore_inactive_pads=True)
        audio_queue = self._make('queue', 'aqueue',
                                 max_size_time=2 * Gst.SECOND,
                                 leaky=2)
        audioconvert = self._make('audioconvert')
        audioresample = self._make('audioresample')
        audio_caps = self._make('capsfilter', 'aaccaps')
        audio_caps.set_property('caps',
                                Gst.Caps.from_string(ENCODER_AUDIO_CAPS))
        self.aacenc = self._make('avenc_aac', bitrate=self.audio_bitrate * 1000)
        aacparse = self._make('aacparse')
        audio_mux_queue = self._make('queue', 'amuxqueue')

        # The audio equivalent of the black layer: silence that never stops.
        # Without this, a source with no audio track starves flvmux forever.
        # Live, like the video base layer -- see RtmpSource._align_to_running_time
        # for why that does not swallow real audio.
        self.audio_base = self._make('audiotestsrc', 'audiobase',
                                     wave='silence', is_live=True)
        silence_caps = self._make('capsfilter', 'silencecaps')
        silence_caps.set_property('caps', Gst.Caps.from_string(AUDIO_CAPS))

        # --- mux + sink ---
        # latency gives flvmux a window to interleave the two branches; live
        # sources arrive with unequal delay and 0 would drop one of them.
        self.flvmux = self._make('flvmux', 'flvmux',
                                 streamable=True,
                                 latency=1 * Gst.SECOND)
        self.rtmpsink = self._make('rtmp2sink', 'rtmpsink',
                                   location=self.output_url)

        log.debug('Linking pipeline elements')
        self._link_many(self.video_base, base_caps)
        # The black layer explicitly owns compositor zorder 0 so it can never
        # occlude a real source. Everything else is offset above it -- see
        # RtmpSource.ZORDER_OFFSET.
        base_pad = self.compositor.request_pad(
            self.compositor.get_pad_template('sink_%u'), None, None)
        if base_pad is None:
            raise RuntimeError('Could not obtain compositor pad for base layer')
        base_pad.set_property('zorder', 0)
        if base_caps.get_static_pad('src').link(base_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError('Could not link black base layer into compositor')
        self._link_many(self.compositor, videoconvert, video_caps, video_queue,
                        self.x264enc, x264_caps, h264parse, video_mux_queue,
                        self.flvmux)
        self._link_many(self.audio_base, silence_caps, self.audiomixer)
        self._link_many(self.audiomixer, audio_queue, audioconvert,
                        audioresample, audio_caps, self.aacenc, aacparse,
                        audio_mux_queue, self.flvmux)
        self._link_many(self.flvmux, self.rtmpsink)

        self._watch_bus()

    @staticmethod
    def _link_many(*elements):
        for upstream, downstream in zip(elements, elements[1:]):
            if not upstream.link(downstream):
                raise RuntimeError('Could not link {} -> {}'.format(
                    upstream.get_name(), downstream.get_name()))

    # -- diagnostics -------------------------------------------------------

    def _watch_bus(self):
        """Surface pipeline errors instead of failing silently."""
        bus = self.pipeline.get_bus()
        self.bus_watch_id = bus.add_watch(GLib.PRIORITY_DEFAULT,
                                          self._on_bus_message, None)

    def _source_for(self, obj):
        """Find the source that owns the element a message came from.

        Errors often originate inside a decodebin, so walk up the parents
        until something matches a source's element list.
        """
        while obj is not None:
            # Snapshot the sources: the API adds and removes them from the
            # aiohttp thread while this runs on the bus watch thread. Ownership
            # itself is queried under each source's own lock.
            for source in (list(self.sources.values()) +
                           list(self._retiring_sources.values())):
                if source.owns(obj):
                    return source
            obj = obj.get_parent()
        return None

    def _on_bus_message(self, bus, message, _data):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            # An error from a source's own elements means that input died --
            # a refused connection, or the idle timeout expiring on a
            # half-open one. Hand it to the source so it reconnects rather
            # than leaving the layer black forever.
            source = self._source_for(message.src)
            if source is not None:
                source.handle_disconnect(err.message)
                return True
            log.error('[%s] %s (from %s)', self.output_url, err.message,
                      message.src.get_name())
            if debug:
                log.error('[%s] debug: %s', self.output_url, debug)
        elif t == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            log.warning('[%s] %s (from %s)', self.output_url, err.message,
                        message.src.get_name())
        elif t == Gst.MessageType.EOS:
            log.warning('[%s] unexpected end of stream', self.output_url)
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src is self.pipeline:
                old, new, _ = message.parse_state_changed()
                log.info('[%s] %s -> %s', self.output_url,
                         old.value_nick, new.value_nick)
        return True
