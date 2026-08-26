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

import rtmpsource

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
        self.output_url = output_url
        self.width = width
        self.height = height
        self.fps = fps
        self.video_bitrate = video_bitrate
        self.audio_bitrate = audio_bitrate
        self.initialize()

    # -- lifecycle ---------------------------------------------------------

    def play(self):
        log.info('Starting pipeline -> %s', self.output_url)
        self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self):
        log.info('Pausing pipeline -> %s', self.output_url)
        self.pipeline.set_state(Gst.State.PAUSED)

    def shutdown(self):
        """Tear the pipeline down and release every source."""
        log.info('Shutting down pipeline -> %s', self.output_url)
        for pip_id in list(self.sources):
            try:
                self.sources[pip_id].remove()
            except Exception:
                log.exception('Error removing source %s', pip_id)
        self.sources.clear()
        if self.bus_watch_id is not None:
            GLib.source_remove(self.bus_watch_id)
            self.bus_watch_id = None
        self.pipeline.set_state(Gst.State.NULL)

    # -- sources -----------------------------------------------------------

    def add_rtmp_source(self, pip_id, location, xpos=0, ypos=0, zorder=1,
                        width=None, height=None):
        if pip_id in self.sources:
            raise ValueError('pip_id={} already exists'.format(pip_id))
        source = rtmpsource.RtmpSource(location, self.pipeline,
                                       self.compositor, self.audiomixer,
                                       xpos, ypos, zorder, width, height)
        self.sources[pip_id] = source
        return source

    def remove_rtmp_source(self, pip_id):
        self._get(pip_id).remove()
        del self.sources[pip_id]

    def resize_rtmp_source(self, pip_id, width, height):
        self._get(pip_id).resize(width, height)

    def move_rtmp_source(self, pip_id, xpos, ypos, zorder):
        self._get(pip_id).move(xpos, ypos, zorder)

    def _get(self, pip_id):
        if pip_id not in self.sources:
            raise KeyError('pip_id={} does not exist'.format(pip_id))
        return self.sources[pip_id]

    def get_info(self):
        return {
            'output_uri': self.output_url,
            'width': self.width,
            'height': self.height,
            'fps': self.fps,
            'state': self.pipeline.get_state(0)[1].value_nick,
            'pip_streams': {pid: s.get_info()
                            for pid, s in self.sources.items()},
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
        # here -- every PiP arrives late.
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

    def _on_bus_message(self, bus, message, _data):
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
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
