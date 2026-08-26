"""Pipeline construction and wiring invariants.

These use real GStreamer rather than mocks on purpose. Every serious bug this
project had -- a caps mismatch the encoder silently rejected, an audio branch
that never reached the muxer, a base layer occluding the background -- lives in
the wiring, and a mock of Gst would have happily accepted all of them.
"""

import pytest

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst  # noqa: E402

import rtmpsource  # noqa: E402
import videomixer  # noqa: E402

from conftest import DEAD_RTMP_URL, gst_list  # noqa: E402


# Every element the mixer and its sources rely on, with the plugin package
# that ships it. A missing one gives a named failure instead of a cryptic
# "could not link" at runtime.
REQUIRED_ELEMENTS = [
    ('compositor', 'plugins-base'),
    ('audiomixer', 'plugins-base'),
    ('videoconvert', 'plugins-base'),
    ('videoscale', 'plugins-base'),
    ('videorate', 'plugins-base'),
    ('audioconvert', 'plugins-base'),
    ('audioresample', 'plugins-base'),
    ('decodebin', 'plugins-base'),
    ('videotestsrc', 'plugins-base'),
    ('audiotestsrc', 'plugins-base'),
    ('queue', 'core'),
    ('capsfilter', 'core'),
    ('fakesink', 'core'),
    ('flvmux', 'plugins-good'),
    ('flvdemux', 'plugins-good'),
    ('aacparse', 'plugins-good'),
    ('h264parse', 'plugins-bad'),
    ('rtmp2src', 'plugins-bad'),
    ('rtmp2sink', 'plugins-bad'),
    ('x264enc', 'plugins-ugly'),
    ('avenc_aac', 'libav'),
]


@pytest.mark.parametrize('name,package', REQUIRED_ELEMENTS)
def test_required_element_is_available(name, package):
    assert Gst.ElementFactory.make(name, None) is not None, (
        '{} is missing -- install gstreamer1.0-{}'.format(name, package))


def test_pipeline_constructs_and_links(mixer):
    """Construction raises if any link fails, so reaching here is the assertion."""
    assert mixer.pipeline is not None
    assert mixer.pipeline.get_state(0)[1] == Gst.State.NULL


def test_output_url_and_geometry_are_applied(mixer):
    assert mixer.rtmpsink.get_property('location') == DEAD_RTMP_URL
    caps = mixer.pipeline.get_by_name('outcaps').get_property('caps')
    structure = caps.get_structure(0)
    assert structure.get_int('width')[1] == 320
    assert structure.get_int('height')[1] == 180


def test_audio_branch_reaches_the_muxer(mixer):
    """The original freeze: audio never actually got linked to flvmux.

    flvmux requests one sink pad per branch, so two pads means both video and
    audio arrive. One pad means the audio branch dead-ends and the muxer will
    either emit no audio or, if it is expecting some, stall the pipeline.
    """
    sink_pads = [p.get_name() for p in gst_list(mixer.flvmux.iterate_sink_pads())]
    assert len(sink_pads) == 2, (
        'flvmux has {} sink pads, expected video and audio: {}'.format(
            len(sink_pads), sink_pads))


def test_encoder_caps_are_f32le(mixer):
    """avenc_aac accepts F32LE only; S16LE here fails to link at construction."""
    caps = mixer.pipeline.get_by_name('aaccaps').get_property('caps')
    structure = caps.get_structure(0)
    assert structure.get_string('format') == 'F32LE'
    assert structure.get_int('rate')[1] == 44100
    assert structure.get_int('channels')[1] == 2


def test_base_layers_exist_and_are_live(mixer):
    """Both base layers keep the aggregators producing when sources are idle."""
    assert mixer.video_base.get_property('is-live') is True
    assert mixer.audio_base.get_property('is-live') is True
    assert mixer.audio_base.get_property('wave').value_nick == 'silence'


def test_black_base_layer_owns_zorder_zero(mixer):
    """The base layer must sit under everything, or it hides the background."""
    base_caps = mixer.pipeline.get_by_name('basecaps')
    peer = base_caps.get_static_pad('src').get_peer()
    assert peer is not None, 'black base layer is not linked into the compositor'
    assert peer.get_property('zorder') == 0


def test_mixers_tolerate_late_and_inactive_sources(mixer):
    """Headroom for sources that join late, and no stalling on dead ones."""
    for element in (mixer.compositor, mixer.audiomixer):
        assert element.get_property('min-upstream-latency') > 0
        assert element.get_property('ignore-inactive-pads') is True


def test_get_info_reports_state_and_geometry(mixer):
    info = mixer.get_info()
    assert info['output_uri'] == DEAD_RTMP_URL
    assert info['width'] == 320
    assert info['height'] == 180
    assert info['state'] == 'null'
    assert info['pip_streams'] == {}


class TestSources:
    def test_add_source_registers_and_rejects_duplicates(self, mixer):
        mixer.add_rtmp_source('bg', DEAD_RTMP_URL)
        assert 'bg' in mixer.sources
        with pytest.raises(ValueError):
            mixer.add_rtmp_source('bg', DEAD_RTMP_URL)

    def test_unknown_pip_id_raises(self, mixer):
        with pytest.raises(KeyError):
            mixer.resize_rtmp_source('nope', 10, 10)
        with pytest.raises(KeyError):
            mixer.move_rtmp_source('nope', 1, 2, 3)
        with pytest.raises(KeyError):
            mixer.remove_rtmp_source('nope')

    def test_source_builds_rtmp2src_not_legacy_rtmpsrc(self, mixer):
        """rtmpsrc fails with 'Internal data stream error' on modern servers."""
        source = mixer.add_rtmp_source('bg', DEAD_RTMP_URL)
        assert source.rtmp_src.get_factory().get_name() == 'rtmp2src'

    def test_remove_source_releases_elements(self, mixer):
        mixer.add_rtmp_source('bg', DEAD_RTMP_URL)
        before = len(gst_list(mixer.pipeline.iterate_elements()))
        mixer.remove_rtmp_source('bg')
        after = len(gst_list(mixer.pipeline.iterate_elements()))
        assert 'bg' not in mixer.sources
        assert after < before, 'remove() left its elements in the pipeline'

    def test_geometry_is_recorded_before_any_data_arrives(self, mixer):
        source = mixer.add_rtmp_source('pip', DEAD_RTMP_URL, xpos=20, ypos=30,
                                       zorder=5, width=160, height=90)
        info = source.get_info()
        assert info['video'] == {'width': 160, 'height': 90,
                                 'xpos': 20, 'ypos': 30, 'zorder': 5}
        # No stream, so nothing has been decoded and no audio pad exists yet.
        assert info['has_audio'] is False
        assert info['source'] == {'width': None, 'height': None}


class TestZOrder:
    """zorder 0 is reserved for the base layer, so sources shift up by one."""

    def _source_with_pad(self, mixer):
        source = mixer.add_rtmp_source('pip', DEAD_RTMP_URL, zorder=1)
        template = mixer.compositor.get_pad_template('sink_%u')
        source.compositor_pad = mixer.compositor.request_pad(template, None, None)
        return source

    def test_offset_keeps_sources_above_the_base_layer(self, mixer):
        source = self._source_with_pad(mixer)
        source.move(0, 0, 0)  # caller's "background" layer
        assert source.compositor_pad.get_property('zorder') == 1
        assert source.zorder == 0, 'caller-facing zorder should stay unchanged'

    def test_move_applies_position_and_offset_zorder(self, mixer):
        source = self._source_with_pad(mixer)
        source.move(40, 50, 7)
        pad = source.compositor_pad
        assert pad.get_property('xpos') == 40
        assert pad.get_property('ypos') == 50
        assert pad.get_property('zorder') == 7 + rtmpsource.RtmpSource.ZORDER_OFFSET

    def test_resize_sets_pad_dimensions(self, mixer):
        source = self._source_with_pad(mixer)
        source.resize(320, 240)
        assert source.compositor_pad.get_property('width') == 320
        assert source.compositor_pad.get_property('height') == 240

    def test_resize_to_none_restores_native_size(self, mixer):
        source = self._source_with_pad(mixer)
        source.resize(None, None)
        # compositor treats 0 as "use the source's own dimensions"
        assert source.compositor_pad.get_property('width') == 0
        assert source.compositor_pad.get_property('height') == 0


def test_move_before_video_arrives_does_not_crash(mixer):
    """The API can be called before a source has decoded anything."""
    source = mixer.add_rtmp_source('pip', DEAD_RTMP_URL)
    assert source.compositor_pad is None
    source.move(1, 2, 3)
    source.resize(10, 20)
    assert source.xpos == 1 and source.ypos == 2 and source.zorder == 3
    assert source.width == 10 and source.height == 20


def test_missing_element_raises_a_useful_error(mixer, monkeypatch):
    monkeypatch.setattr(Gst.ElementFactory, 'make',
                        staticmethod(lambda *a, **k: None))
    with pytest.raises(RuntimeError, match='unavailable'):
        videomixer.VideoMixer(DEAD_RTMP_URL)
