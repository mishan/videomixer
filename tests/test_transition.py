"""Keyframe arithmetic and the compositor binding lifecycle."""

import threading

import pytest
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst  # noqa: E402

import layout
import transition

from conftest import DEAD_RTMP_URL


def test_plan_eases_geometry_and_fades_new_sources():
    destination = {'a': layout.Cell(100, 20, 200, 100, 1, 'contain', 1.0),
                   'b': layout.Cell(0, 0, 50, 50, 1, 'contain', 1.0)}
    starts = {'a': {'xpos': 0, 'ypos': 20, 'width': 100,
                    'height': 100, 'alpha': 1.0}, 'b': None}
    frames = transition.plan(starts, destination, 0.4, 'ease-in-out')
    assert frames['a']['xpos'][0] == (0, 0.0)
    assert frames['a']['xpos'][10] == pytest.approx((0.2, 50))
    assert frames['a']['xpos'][-1] == pytest.approx((0.4, 100))
    assert 'ypos' not in frames['a']
    assert set(frames['b']) == {'alpha'}
    assert frames['b']['alpha'][0] == (0, 0.0)
    assert frames['b']['alpha'][-1] == pytest.approx((0.4, 1.0))


@pytest.mark.parametrize('bad', [
    None, 1, {'duration': 0}, {'duration': 3}, {'duration': float('nan')},
    {'duration': True}, {'duration': 'fast'}, {'duration': 10 ** 400},
    {'easing': 'bounce'},
    {'unknown': 1},
])
def test_invalid_transition_rejects_the_layout(bad):
    with pytest.raises(layout.LayoutError, match='transition'):
        layout.resolve({'preset': 'grid', 'transition': bad}, 320, 180, ['a'])


def _with_pad(mixer, source_id):
    source = mixer.add_rtmp_source(source_id, DEAD_RTMP_URL)
    template = mixer.compositor.get_pad_template('sink_%u')
    source.compositor_pad = mixer.compositor.request_pad(template, None, None)
    source._apply_geometry()
    return source


def test_bindings_interpolate_and_settle_on_real_compositor_pad(mixer):
    source = _with_pad(mixer, 'a')
    source.resize(100, 80)
    mixer.set_layout({'preset': 'grid', 'transition': {
        'duration': 0.4, 'easing': 'linear'}})
    pad = source.compositor_pad
    assert source._transition_bindings
    pad.sync_values(200_000_000)
    assert pad.get_property('width') == 210
    source._finish_transition(source._transition_generation, pad,
                              mixer.resolved_layout()['a'])
    assert not source._transition_bindings
    assert pad.get_property('width') == 320
    source.resize(40, 30)
    assert pad.get_property('width') == 40


def test_interrupt_and_manual_edit_cancel_old_bindings(mixer):
    source = _with_pad(mixer, 'a')
    mixer.set_layout({'cells': [{'source': 'a', 'x': 200, 'y': 0,
                                 'width': 100, 'height': 100}],
                      'transition': {'duration': 0.4, 'easing': 'linear'}})
    pad = source.compositor_pad
    pad.sync_values(200_000_000)
    midpoint = pad.get_property('xpos')
    mixer.set_layout({'cells': [{'source': 'a', 'x': 0, 'y': 0,
                                 'width': 100, 'height': 100}],
                      'transition': {'duration': 0.4, 'easing': 'linear'}})
    assert pad.get_property('xpos') == midpoint
    mixer.move_rtmp_source('a', 7, 8, 1)
    assert not source._transition_bindings
    assert pad.get_property('xpos') == 7
    assert mixer.layout is None


def test_clearing_layout_freezes_in_flight_position(mixer):
    source = _with_pad(mixer, 'a')
    mixer.set_layout({'cells': [{'source': 'a', 'x': 200, 'y': 0,
                                 'width': 100, 'height': 100}],
                      'transition': {'duration': 0.4, 'easing': 'linear'}})
    pad = source.compositor_pad
    pad.sync_values(200_000_000)
    midpoint = pad.get_property('xpos')
    assert 0 < midpoint < 200
    generation = source._transition_generation
    mixer.clear_layout()
    assert mixer.layout is None
    assert not source._transition_bindings
    assert source.xpos == pad.get_property('xpos') == midpoint
    pad.sync_values(400_000_000)
    assert pad.get_property('xpos') == midpoint
    assert not source._settle_transition(generation, pad, None, 0)
    assert pad.get_property('xpos') == midpoint


def test_replacement_layout_freezes_an_omitted_source(mixer):
    source = _with_pad(mixer, 'a')
    _with_pad(mixer, 'b')
    mixer.set_layout({'cells': [{'source': 'a', 'x': 200, 'y': 0,
                                 'width': 100, 'height': 100}],
                      'transition': {'duration': 0.4, 'easing': 'linear'}})
    pad = source.compositor_pad
    pad.sync_values(200_000_000)
    midpoint = pad.get_property('xpos')
    mixer.set_layout({'cells': [{'source': 'b', 'x': 20, 'y': 0,
                                 'width': 100, 'height': 100}]})
    assert not source._transition_bindings
    assert source.xpos == pad.get_property('xpos') == midpoint
    pad.sync_values(400_000_000)
    assert pad.get_property('xpos') == midpoint


def test_settlement_waits_for_pipeline_running_time(mixer, monkeypatch):
    source = _with_pad(mixer, 'a')

    class Clock:
        now = 0

        def get_time(self):
            return self.now

    class Pipeline:
        state = Gst.State.PLAYING
        clock = Clock()

        def get_clock(self):
            return self.clock

        def get_base_time(self):
            return 0

        def get_state(self, timeout):
            return None, self.state, None

    scheduled = []

    def timeout_add(interval, callback, *args):
        scheduled.append((interval, callback, args))
        return 1

    monkeypatch.setattr('rtmpsource.GLib.timeout_add', timeout_add)
    source.pipeline = Pipeline()
    try:
        mixer.set_layout({'cells': [{'source': 'a', 'x': 200, 'y': 0,
                                     'width': 100, 'height': 100}],
                          'transition': {'duration': 0.4, 'easing': 'linear'}})
        _, callback, args = scheduled[-1]
        source.pipeline.clock.now = 200_000_000
        source.compositor_pad.sync_values(200_000_000)
        assert source.compositor_pad.get_property('xpos') == 100

        source.pipeline.state = Gst.State.PAUSED
        assert callback(*args)
        assert source._transition_bindings

        source.pipeline.state = Gst.State.PLAYING
        assert callback(*args)
        assert source._transition_bindings

        source.pipeline.clock.now = 500_000_000
        assert not callback(*args)
        assert not source._transition_bindings
        assert source.compositor_pad.get_property('xpos') == 200
    finally:
        source.pipeline = mixer.pipeline


def test_membership_changes_animate_existing_cells_and_remove_immediately(mixer):
    first = _with_pad(mixer, 'a')
    mixer.set_layout({'preset': 'row', 'transition': {'duration': 0.4}})
    second = mixer.add_rtmp_source('b', DEAD_RTMP_URL)
    assert first.width == second.width == 160
    assert first._transition_bindings
    assert second._pending_transition is not None
    mixer.remove_rtmp_source('b')
    assert 'b' not in mixer.sources
    assert first.width == 320
    assert first.compositor_pad.get_property('width') == 320


def test_waiting_new_source_keeps_its_fade_when_membership_changes(mixer):
    mixer.set_layout({'preset': 'grid', 'transition': {'duration': 0.4}})
    first = mixer.add_rtmp_source('a', DEAD_RTMP_URL)
    mixer.add_rtmp_source('b', DEAD_RTMP_URL)
    assert first._pending_transition is not None
    frames, cell, _ = first._pending_transition
    assert cell.width == 160
    assert frames['alpha'][0][1] == 0.0


def test_solo_hides_with_alpha_animation(mixer):
    first = _with_pad(mixer, 'a')
    hidden = _with_pad(mixer, 'b')
    mixer.set_layout({'preset': 'solo', 'source': 'a',
                      'transition': {'duration': 0.4}})
    assert first.compositor_pad is not None
    assert hidden._transition_bindings
    assert hidden.alpha == 0.0
    assert hidden.compositor_pad.get_property('alpha') == 1.0


def test_zorder_rises_before_animation_and_falls_after_settlement(mixer):
    source = _with_pad(mixer, 'a')
    rising = {'cells': [{'source': 'a', 'x': 100, 'y': 0,
                         'width': 100, 'height': 100, 'z': 3}],
              'transition': {'duration': 0.4}}
    mixer.set_layout(rising)
    pad = source.compositor_pad
    assert pad.get_property('zorder') == 3 + source.ZORDER_OFFSET

    falling = {'cells': [{'source': 'a', 'x': 0, 'y': 0,
                          'width': 100, 'height': 100, 'z': 0}],
               'transition': {'duration': 0.4}}
    mixer.set_layout(falling)
    assert pad.get_property('zorder') == 3 + source.ZORDER_OFFSET
    source._finish_transition(source._transition_generation, pad,
                              mixer.resolved_layout()['a'])
    assert pad.get_property('zorder') == source.ZORDER_OFFSET


def test_new_source_fades_and_reconnect_keeps_final_geometry(mixer):
    mixer.set_layout({'preset': 'grid', 'transition': {'duration': 0.4}})
    source = mixer.add_rtmp_source('a', DEAD_RTMP_URL)
    assert source._pending_transition is not None
    template = mixer.compositor.get_pad_template('sink_%u')
    source.compositor_pad = mixer.compositor.request_pad(template, None, None)
    frames, cell, duration = source._pending_transition
    source.start_transition(frames, cell, duration)
    assert source.compositor_pad.get_property('alpha') == 0.0
    source._teardown_elements()
    assert not source._transition_bindings
    assert source._pending_transition is None
    source.compositor_pad = mixer.compositor.request_pad(template, None, None)
    source._apply_geometry()
    assert source.compositor_pad.get_property('alpha') == 1.0
    assert source.compositor_pad.get_property('width') == 320


def test_clear_before_first_frame_cancels_pending_fade(mixer):
    mixer.set_layout({'preset': 'grid', 'transition': {'duration': 0.4}})
    source = mixer.add_rtmp_source('a', DEAD_RTMP_URL)
    assert source._pending_transition is not None
    mixer.clear_layout()
    pad = source._attach_compositor_pad()
    assert pad is not None
    assert pad.get_property('alpha') == 1.0
    assert not source._transition_bindings
    assert source._transition_timer is None


def test_replacement_before_first_frame_cancels_omitted_fade(mixer):
    mixer.set_layout({'preset': 'grid', 'transition': {'duration': 0.4}})
    source = mixer.add_rtmp_source('a', DEAD_RTMP_URL)
    assert source._pending_transition is not None
    mixer.set_layout({'cells': []})
    pad = source._attach_compositor_pad()
    assert pad is not None
    assert pad.get_property('alpha') == 1.0
    assert not source._transition_bindings
    assert source._transition_timer is None


def test_clear_during_first_frame_cannot_restart_canceled_fade(mixer, monkeypatch):
    mixer.set_layout({'preset': 'grid', 'transition': {'duration': 0.4}})
    source = mixer.add_rtmp_source('a', DEAD_RTMP_URL)
    applied = threading.Event()
    proceed = threading.Event()
    clearing = threading.Event()
    cleared = threading.Event()
    errors = []
    apply_geometry = source._apply_geometry

    def pause_after_pending_is_cleared():
        apply_geometry()
        applied.set()
        assert proceed.wait(2)

    def attach():
        try:
            source._attach_compositor_pad()
        except Exception as exc:
            errors.append(exc)

    def clear():
        clearing.set()
        try:
            mixer.clear_layout()
        except Exception as exc:
            errors.append(exc)
        finally:
            cleared.set()

    monkeypatch.setattr(source, '_apply_geometry', pause_after_pending_is_cleared)
    attaching = threading.Thread(target=attach)
    canceling = threading.Thread(target=clear)
    attaching.start()
    try:
        assert applied.wait(2)
        canceling.start()
        assert clearing.wait(2)
        assert not cleared.wait(0.1), 'layout clear overtook first-frame setup'
    finally:
        proceed.set()
        attaching.join(2)
        if canceling.ident is not None:
            canceling.join(2)

    assert not attaching.is_alive() and not canceling.is_alive()
    assert not errors
    assert mixer.layout is None
    assert source._pending_transition is None
    assert not source._transition_bindings
    assert source._transition_timer is None


def test_teardown_waits_for_binding_installation(mixer, monkeypatch):
    source = _with_pad(mixer, 'a')
    destination = layout.Cell(200, 10, 100, 100, 1, 'contain', 0.5)
    frames = transition.plan({'a': source.current_transition_cell(320, 180)},
                             {'a': destination}, 0.4, 'linear')['a']
    installing = threading.Event()
    proceed = threading.Event()
    removing = threading.Event()
    removed = threading.Event()
    errors = []
    apply_fit = source._apply_fit

    def pause_during_install(pad):
        installing.set()
        assert proceed.wait(2)
        apply_fit(pad)

    def install():
        try:
            source.start_transition(frames, destination, 0.4)
        except Exception as exc:
            errors.append(exc)

    def teardown():
        removing.set()
        try:
            source._teardown_elements()
        except Exception as exc:
            errors.append(exc)
        finally:
            removed.set()

    monkeypatch.setattr(source, '_apply_fit', pause_during_install)
    installer = threading.Thread(target=install)
    remover = threading.Thread(target=teardown)
    installer.start()
    try:
        assert installing.wait(2)
        remover.start()
        assert removing.wait(2)
        assert not removed.wait(0.1), 'pad teardown overtook binding installation'
    finally:
        proceed.set()
        installer.join(2)
        if remover.ident is not None:
            remover.join(2)

    assert not installer.is_alive() and not remover.is_alive()
    assert not errors
    assert source.compositor_pad is None
    assert not source._transition_bindings
    assert source._transition_timer is None


def test_frame_mid_transition_contains_the_moving_source(mixer):
    """Inspect actual output pixels, including the frame between endpoints."""
    source = mixer.add_rtmp_source('a', DEAD_RTMP_URL)
    pipeline = Gst.Pipeline.new('transition-frames')

    def element(factory):
        item = Gst.ElementFactory.make(factory, None)
        assert item is not None
        pipeline.add(item)
        return item

    image = element('videotestsrc')
    image.set_property('pattern', 'white')
    image.set_property('num-buffers', 14)
    image_caps = element('capsfilter')
    image_caps.set_property('caps', Gst.Caps.from_string(
        'video/x-raw,format=RGB,width=40,height=40,framerate=20/1'))
    compositor = element('compositor')
    compositor.set_property('background', 'black')
    convert = element('videoconvert')
    output_caps = element('capsfilter')
    output_caps.set_property('caps', Gst.Caps.from_string(
        'video/x-raw,format=RGB,width=100,height=40,framerate=20/1'))
    sink = element('appsink')
    sink.set_property('sync', False)
    assert image.link(image_caps)
    pad = compositor.request_pad(compositor.get_pad_template('sink_%u'),
                                 None, None)
    assert image_caps.get_static_pad('src').link(pad) == Gst.PadLinkReturn.OK
    assert compositor.link(convert) and convert.link(output_caps)
    assert output_caps.link(sink)

    source.pipeline = pipeline
    source.compositor = compositor
    source.compositor_pad = pad
    source.set_cell(layout.Cell(0, 0, 40, 40, 1, 'fill', 1.0))
    destination = layout.Cell(60, 0, 40, 40, 1, 'fill', 1.0)
    frames = transition.plan({'a': source.current_transition_cell(100, 40)},
                             {'a': destination}, 0.4, 'linear')['a']
    source.start_transition(frames, destination, 0.4)
    try:
        assert pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        positions = []
        for _ in range(14):
            sample = sink.emit('try-pull-sample', Gst.SECOND)
            assert sample is not None
            data = sample.get_buffer().extract_dup(0, 100 * 40 * 3)
            row = data[20 * 100 * 3:(20 + 1) * 100 * 3]
            positions.append(next(x for x in range(100)
                                  if row[3 * x] > 200))
        assert positions[0] <= 2
        assert 22 <= positions[4] <= 38
        assert positions[10] >= 58
    finally:
        source._drop_transition()
        pipeline.set_state(Gst.State.NULL)
        source.compositor_pad = None
        source.pipeline = mixer.pipeline
        source.compositor = mixer.compositor
