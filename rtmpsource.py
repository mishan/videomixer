#!/usr/bin/env python3
"""A single RTMP input feeding a VideoMixer's compositor and audiomixer.

Everything here is built dynamically. rtmp2src -> flvdemux exposes its audio
and video pads only once the stream is actually flowing, and decodebin in turn
exposes its decoded pads later still, so the chain is assembled from inside
pad-added callbacks.

Two rules matter and both were violated by the original implementation:

1. Every pad a demuxer exposes must be consumed. An unlinked pad returns
   GST_FLOW_NOT_LINKED, which propagates upstream and takes the entire
   pipeline down with an opaque "Internal data stream error". Pads we have no
   use for get a fakesink rather than being ignored.

2. Elements added to an already-PLAYING pipeline start life in NULL state and
   will never produce data until told otherwise. Every element added here is
   followed by sync_state_with_parent(), which is what makes it possible to
   add a PiP to a running stream.
"""

import logging

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst  # noqa: E402

log = logging.getLogger(__name__)

AUDIO_CAPS = 'audio/x-raw,rate=44100,channels=2,format=S16LE,layout=interleaved'


class RtmpSource:
    # compositor zorder 0 is reserved for the mixer's always-black base layer,
    # so every real source is shifted one step up. Callers keep using the
    # documented scheme (background z=0, PiPs z>=1) and self.zorder always
    # holds that caller-facing value.
    ZORDER_OFFSET = 1

    def __init__(self, location, pipeline, compositor, audiomixer,
                 xpos=0, ypos=0, zorder=1, width=None, height=None):
        self.location = location
        self.pipeline = pipeline
        self.compositor = compositor
        self.audiomixer = audiomixer

        self.xpos = xpos
        self.ypos = ypos
        self.zorder = zorder
        self.width = width
        self.height = height

        # Native dimensions, discovered from the decoded caps.
        self.video_width = None
        self.video_height = None

        # Request pads we hold on the mixers, released on teardown.
        self.compositor_pad = None
        self.audiomixer_pad = None
        # Every element we own, so removal is exact.
        self.elements = []

        self.initialize()

    # -- construction ------------------------------------------------------

    def _align_to_running_time(self, pad):
        """Shift a late-joining source into the mixer's current running time.

        Both mixers are aggregators driven by live base layers, so by the time
        an RTMP source has connected, demuxed and decoded -- a second or so --
        the aggregator has already advanced. A newly linked pad delivers
        buffers timestamped from the start of *its* stream, which lands in the
        aggregator's past and gets discarded. The symptom is a source that
        decodes without a single error yet contributes nothing: silent audio,
        or a layer that never appears.

        Whether it happened at all came down to timing, which is why this
        looked intermittent. Offsetting the pad by the running time at which
        the source joined maps its stream start onto now, deterministically.
        """
        clock = self.pipeline.get_clock()
        if clock is None:
            return  # not started yet; stream time already lines up
        running_time = clock.get_time() - self.pipeline.get_base_time()
        if running_time <= 0:
            return
        pad.set_offset(running_time)
        log.debug('[%s] offset %s by %.3fs', self.location, pad.get_name(),
                  running_time / Gst.SECOND)

    def _make(self, factory, **props):
        element = Gst.ElementFactory.make(factory, None)
        if element is None:
            raise RuntimeError(
                'GStreamer element "{}" is unavailable -- is the matching '
                'gst-plugins package installed?'.format(factory))
        for key, value in props.items():
            element.set_property(key.replace('_', '-'), value)
        self.pipeline.add(element)
        self.elements.append(element)
        # Required when the pipeline is already running; harmless when it
        # is not.
        element.sync_state_with_parent()
        return element

    def initialize(self):
        log.info('Creating RTMP source for %s', self.location)
        # rtmp2src supersedes the librtmp-based rtmpsrc, which fails with
        # "Internal data stream error" against current RTMP servers.
        self.rtmp_src = self._make('rtmp2src', location=self.location)
        self.flvdemux = self._make('flvdemux')
        self.flvdemux.connect('pad-added', self._on_demux_pad_added)

        if not self.rtmp_src.link(self.flvdemux):
            raise RuntimeError('Could not link rtmp2src -> flvdemux')

    # -- dynamic linking ---------------------------------------------------

    def _on_demux_pad_added(self, demux, pad):
        caps = pad.get_current_caps()
        pad_type = caps.get_structure(0).get_name() if caps else ''
        log.info('[%s] demux pad %s (%s)', self.location, pad.get_name(),
                 pad_type or 'unknown')

        if pad_type.startswith('video'):
            sink_pad = self._build_video_branch()
        elif pad_type.startswith('audio'):
            sink_pad = self._build_audio_branch()
        else:
            # Rule 1: consume it anyway, or it stalls everything.
            log.info('[%s] discarding unhandled pad type %s',
                     self.location, pad_type)
            sink_pad = self._make('fakesink', sync=False,
                                  async_=False).get_static_pad('sink')

        if sink_pad is None or sink_pad.is_linked():
            return
        result = pad.link(sink_pad)
        if result != Gst.PadLinkReturn.OK:
            log.error('[%s] failed to link demux pad %s: %s',
                      self.location, pad.get_name(), result.value_nick)

    def _build_video_branch(self):
        queue = self._make('queue', max_size_time=2 * Gst.SECOND, leaky=2)
        decodebin = self._make('decodebin')
        decodebin.connect('pad-added', self._on_video_decoded)
        # decodebin can also decide a stream is undecodable; make sure that
        # does not leave a dangling pad.
        decodebin.connect('no-more-pads', lambda *_: None)
        if not queue.link(decodebin):
            raise RuntimeError('Could not link video queue -> decodebin')
        return queue.get_static_pad('sink')

    def _build_audio_branch(self):
        queue = self._make('queue', max_size_time=2 * Gst.SECOND, leaky=2)
        decodebin = self._make('decodebin')
        decodebin.connect('pad-added', self._on_audio_decoded)
        if not queue.link(decodebin):
            raise RuntimeError('Could not link audio queue -> decodebin')
        return queue.get_static_pad('sink')

    def _on_video_decoded(self, decodebin, pad):
        caps = pad.get_current_caps()
        if caps is None or not caps.get_structure(0).get_name().startswith('video'):
            return
        structure = caps.get_structure(0)
        ok, self.video_width = structure.get_int('width')
        ok, self.video_height = structure.get_int('height')
        log.info('[%s] decoded video %sx%s', self.location,
                 self.video_width, self.video_height)

        convert = self._make('videoconvert')
        scale = self._make('videoscale')
        rate = self._make('videorate')

        pad_template = self.compositor.get_pad_template('sink_%u')
        self.compositor_pad = self.compositor.request_pad(pad_template,
                                                          None, None)
        if self.compositor_pad is None:
            log.error('[%s] could not obtain a compositor sink pad',
                      self.location)
            return

        self._align_to_running_time(self.compositor_pad)
        self.compositor_pad.set_property('xpos', self.xpos)
        self.compositor_pad.set_property('ypos', self.ypos)
        self.compositor_pad.set_property('zorder',
                                         self.zorder + self.ZORDER_OFFSET)
        # compositor scales on the pad itself; 0 means "use the native size".
        self.compositor_pad.set_property('width', self.width or 0)
        self.compositor_pad.set_property('height', self.height or 0)

        if pad.link(convert.get_static_pad('sink')) != Gst.PadLinkReturn.OK:
            log.error('[%s] could not link decoded video pad', self.location)
            return
        if not convert.link(scale) or not scale.link(rate):
            log.error('[%s] could not link video conversion chain',
                      self.location)
            return
        if rate.get_static_pad('src').link(self.compositor_pad) != Gst.PadLinkReturn.OK:
            log.error('[%s] could not link into compositor', self.location)

    def _on_audio_decoded(self, decodebin, pad):
        caps = pad.get_current_caps()
        if caps is None or not caps.get_structure(0).get_name().startswith('audio'):
            return
        log.info('[%s] decoded audio', self.location)

        convert = self._make('audioconvert')
        resample = self._make('audioresample')
        capsfilter = self._make('capsfilter')
        capsfilter.set_property('caps', Gst.Caps.from_string(AUDIO_CAPS))

        pad_template = self.audiomixer.get_pad_template('sink_%u')
        self.audiomixer_pad = self.audiomixer.request_pad(pad_template,
                                                           None, None)
        if self.audiomixer_pad is None:
            log.error('[%s] could not obtain an audiomixer sink pad',
                      self.location)
            return

        self._align_to_running_time(self.audiomixer_pad)

        if pad.link(convert.get_static_pad('sink')) != Gst.PadLinkReturn.OK:
            log.error('[%s] could not link decoded audio pad', self.location)
            return
        if not convert.link(resample) or not resample.link(capsfilter):
            log.error('[%s] could not link audio conversion chain',
                      self.location)
            return
        if capsfilter.get_static_pad('src').link(self.audiomixer_pad) != Gst.PadLinkReturn.OK:
            log.error('[%s] could not link into audiomixer', self.location)

    # -- control -----------------------------------------------------------

    def move(self, xpos, ypos, zorder):
        self.xpos, self.ypos, self.zorder = xpos, ypos, zorder
        if self.compositor_pad is None:
            return
        self.compositor_pad.set_property('xpos', xpos)
        self.compositor_pad.set_property('ypos', ypos)
        self.compositor_pad.set_property('zorder', zorder + self.ZORDER_OFFSET)

    def shift(self, xdiff, ydiff, zdiff=0):
        width = self.video_width or 1
        height = self.video_height or 1
        self.move((self.xpos + xdiff) % width,
                  (self.ypos + ydiff) % height,
                  self.zorder + zdiff)

    def resize(self, width, height):
        self.width, self.height = width, height
        if self.compositor_pad is None:
            return
        self.compositor_pad.set_property('width', width or 0)
        self.compositor_pad.set_property('height', height or 0)

    def remove(self):
        """Detach this source and free everything it owns."""
        log.info('Removing RTMP source %s', self.location)
        for element in self.elements:
            element.set_state(Gst.State.NULL)
        for element in self.elements:
            self.pipeline.remove(element)
        self.elements = []

        if self.compositor_pad is not None:
            self.compositor.release_request_pad(self.compositor_pad)
            self.compositor_pad = None
        if self.audiomixer_pad is not None:
            self.audiomixer.release_request_pad(self.audiomixer_pad)
            self.audiomixer_pad = None

    def get_info(self):
        return {
            'location': self.location,
            'source': {
                'width': self.video_width,
                'height': self.video_height,
            },
            'video': {
                'width': self.width,
                'height': self.height,
                'xpos': self.xpos,
                'ypos': self.ypos,
                'zorder': self.zorder,
            },
            'has_audio': self.audiomixer_pad is not None,
        }
