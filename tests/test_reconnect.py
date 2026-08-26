"""Reconnect state machine.

A dropped publisher is invisible at the pipeline level -- the mixer's base
layers never end, so no EOS reaches the bus. Detection hangs off a pad probe
instead, and these cover the machinery that probe drives.

GLib's idle and timer callbacks are intercepted so the sequence can be stepped
through deterministically without a main loop: idle callbacks run inline, and
scheduled retries are recorded rather than armed.
"""

import pytest

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib  # noqa: E402

import rtmpsource  # noqa: E402

from conftest import DEAD_RTMP_URL, gst_list  # noqa: E402


def simulate_stream_arriving(mixer, source):
    """Stand in for the mixer pads that only appear once data is decoded.

    Nothing is publishing in a unit test, so _on_video_decoded never fires and
    the request pads a real source would hold are never taken. Flapping is
    precisely about acquiring and releasing those pads repeatedly, so they have
    to exist for the test to mean anything.
    """
    video_template = mixer.compositor.get_pad_template('sink_%u')
    source.compositor_pad = mixer.compositor.request_pad(
        video_template, None, None)
    audio_template = mixer.audiomixer.get_pad_template('sink_%u')
    source.audiomixer_pad = mixer.audiomixer.request_pad(
        audio_template, None, None)
    source._mark_connected()


@pytest.fixture
def scheduler(monkeypatch):
    """Run idle callbacks inline and capture timer callbacks."""
    class Scheduler:
        def __init__(self):
            self.timers = []      # (delay, callback)
            self.removed = []
            self._next_id = 1

        def timeout_add(self, delay_ms, callback, *args):
            self.timers.append((delay_ms / 1000.0, callback))
            self._next_id += 1
            return self._next_id

        def idle_add(self, callback, *args):
            callback(*args)
            return 0

        def source_remove(self, tag):
            self.removed.append(tag)
            return True

        @property
        def delays(self):
            """Scheduled delays, in seconds. Jittered, so compare as ranges."""
            return [d for d, _ in self.timers]

        def fire_last(self):
            """Run the most recently scheduled retry."""
            _, callback = self.timers[-1]
            return callback()

    s = Scheduler()
    monkeypatch.setattr(GLib, 'timeout_add', s.timeout_add)
    monkeypatch.setattr(GLib, 'idle_add', s.idle_add)
    monkeypatch.setattr(GLib, 'source_remove', s.source_remove)
    return s


@pytest.fixture
def source(mixer, scheduler):
    s = mixer.add_rtmp_source('bg', DEAD_RTMP_URL, xpos=10, ypos=20,
                              zorder=3, width=160, height=90)
    yield s
    # Retire the source while GLib is still patched. The mixer fixture is torn
    # down after this one, and by then the real GLib.source_remove is back and
    # would warn about the fake timer ids handed out here.
    s.remove()
    mixer.sources.pop('bg', None)


def test_starts_out_connecting(source):
    assert source.state == rtmpsource.CONNECTING
    assert source.reconnect_attempts == 0
    assert source.last_error is None


def test_idle_timeout_is_set(source):
    """A half-open connection yields no EOS and no error without this."""
    assert source.rtmp_src.get_property('idle-timeout') == \
        rtmpsource.RtmpSource.IDLE_TIMEOUT


def expected_range(attempt):
    """The window a jittered delay for this attempt must fall in."""
    src = rtmpsource.RtmpSource
    base = min(src.RECONNECT_INITIAL_DELAY * (2 ** attempt),
               src.RECONNECT_MAX_DELAY)
    return base * (1 - src.RECONNECT_JITTER), base


def test_disconnect_records_state_and_schedules_a_retry(source, scheduler):
    source.handle_disconnect('publisher ended the stream')
    assert source.state == rtmpsource.RECONNECTING
    assert source.last_error == 'publisher ended the stream'
    assert source.reconnect_attempts == 1
    low, high = expected_range(0)
    assert low <= scheduler.delays[0] <= high


def test_backoff_is_exponential_and_capped(source, scheduler):
    """1, 2, 4, 8, then flat at the cap -- forever, never giving up."""
    source.handle_disconnect('gone')
    for _ in range(8):
        scheduler.fire_last()          # retry runs, builds elements again
        source.handle_disconnect('still gone')

    for attempt, delay in enumerate(scheduler.delays):
        low, high = expected_range(attempt)
        assert low <= delay <= high, \
            'attempt {} delay {:.2f} outside {:.2f}-{:.2f}'.format(
                attempt, delay, low, high)

    cap = rtmpsource.RtmpSource.RECONNECT_MAX_DELAY
    assert all(d <= cap for d in scheduler.delays)
    # Growing, allowing for jitter overlap between neighbouring attempts.
    assert scheduler.delays[-1] > scheduler.delays[0]


def test_worst_case_recovery_latency_stays_bounded():
    """The cap is how long a layer can stay black after its source returns.

    Pinned rather than derived, because every other assertion here reads the
    constant and would happily follow it upwards. Raising this trades away
    recovery time on live video: at 30s a source cycling faster than the delay
    had every retry land in one of its gaps and went unrecovered for cycles.
    """
    assert rtmpsource.RtmpSource.RECONNECT_MAX_DELAY <= 10.0


class TestJitter:
    """Without jitter, every source dropped by one outage retries in unison."""

    def test_delay_never_exceeds_the_backoff_for_that_attempt(self):
        for attempt in range(8):
            low, high = expected_range(attempt)
            for _ in range(500):
                delay = rtmpsource.RtmpSource.backoff_delay(attempt)
                assert low <= delay <= high

    def test_delays_are_actually_spread_out(self):
        """A constant here would mean the jitter is not doing anything."""
        delays = {rtmpsource.RtmpSource.backoff_delay(4) for _ in range(200)}
        assert len(delays) > 100, 'delays are not being randomised'

    def test_jitter_keeps_the_back_half_of_the_interval(self):
        """Bounds check against a stubbed RNG, both extremes."""
        src = rtmpsource.RtmpSource
        base = src.RECONNECT_MAX_DELAY
        assert src.backoff_delay(9, rand=lambda: 0.0) == base
        assert src.backoff_delay(9, rand=lambda: 1.0) == \
            base * (1 - src.RECONNECT_JITTER)

    def test_two_sources_do_not_line_up(self, mixer, scheduler):
        """The lockstep this exists to prevent."""
        sources = [mixer.add_rtmp_source('s%d' % i, DEAD_RTMP_URL)
                   for i in range(8)]
        for source in sources:
            source.handle_disconnect('shared outage')
        assert len(set(scheduler.delays)) > 1, \
            'every source retried on the same schedule'
        for source in sources:
            source.remove()


def test_a_failed_retry_arms_the_next_one(source, scheduler):
    """The retry loop used to strand itself after a single attempt.

    handle_disconnect keyed off the state, so once it was RECONNECTING the
    failure of the retry itself was swallowed and nothing rescheduled.
    """
    source.handle_disconnect('gone')
    assert len(scheduler.timers) == 1
    scheduler.fire_last()
    source.handle_disconnect('connection refused')
    assert len(scheduler.timers) == 2, 'a failed retry did not schedule another'


def test_reconnecting_rebuilds_the_elements(source, scheduler):
    source.handle_disconnect('gone')
    assert source.elements == [], 'teardown should release the old elements'
    assert source.compositor_pad is None
    assert source.audiomixer_pad is None
    scheduler.fire_last()
    assert source.elements, 'retry should have rebuilt the source'
    assert source.rtmp_src.get_factory().get_name() == 'rtmp2src'


def test_geometry_survives_a_reconnect(source, scheduler):
    """The layer has to come back where the caller put it."""
    source.handle_disconnect('gone')
    scheduler.fire_last()
    assert (source.xpos, source.ypos, source.zorder) == (10, 20, 3)
    assert (source.width, source.height) == (160, 90)


def test_recovery_clears_the_error_and_attempt_count(source, scheduler):
    source.handle_disconnect('gone')
    scheduler.fire_last()
    source._mark_connected()
    assert source.state == rtmpsource.CONNECTED
    assert source.reconnect_attempts == 0
    assert source.last_error is None


def test_removed_source_stops_reconnecting(source, scheduler):
    """Removing a source mid-outage must not leave a retry loop running."""
    source.handle_disconnect('gone')
    before = len(scheduler.timers)
    source.remove()
    source.handle_disconnect('gone again')
    assert len(scheduler.timers) == before, 'kept retrying after removal'
    assert scheduler.removed, 'pending retry timer was not cancelled'


def test_removal_during_backoff_stops_the_pending_retry(source, scheduler):
    source.handle_disconnect('gone')
    source.remove()
    assert scheduler.fire_last() == GLib.SOURCE_REMOVE
    assert source.elements == [], 'a cancelled retry rebuilt the source anyway'


def test_connection_state_is_reported(source, scheduler):
    assert source.get_info()['connection'] == {
        'state': rtmpsource.CONNECTING,
        'reconnect_attempts': 0,
        'last_error': None,
    }
    source.handle_disconnect('publisher ended the stream')
    connection = source.get_info()['connection']
    assert connection['state'] == rtmpsource.RECONNECTING
    assert connection['reconnect_attempts'] == 1
    assert connection['last_error'] == 'publisher ended the stream'


class TestFlapping:
    """A source that keeps dropping and coming back every few seconds.

    Each cycle takes and gives back a request pad on both mixers and rebuilds
    the whole element chain, so this is where leaks would show up: pads that
    are never released accumulate silently and the compositor keeps compositing
    layers that no longer have a source behind them.
    """

    FLAPS = 15

    def _flap_once(self, mixer, source, scheduler):
        simulate_stream_arriving(mixer, source)
        source.handle_disconnect('publisher ended the stream')
        scheduler.fire_last()

    def test_compositor_pads_do_not_accumulate(self, mixer, source, scheduler):
        # One sink pad for the black base layer, plus one per live source.
        base_pads = len(gst_list(mixer.compositor.iterate_sink_pads()))
        for _ in range(self.FLAPS):
            self._flap_once(mixer, source, scheduler)
            assert len(gst_list(mixer.compositor.iterate_sink_pads())) == base_pads, \
                'a compositor pad was left behind by a reconnect'

    def test_audiomixer_pads_do_not_accumulate(self, mixer, source, scheduler):
        base_pads = len(gst_list(mixer.audiomixer.iterate_sink_pads()))
        for _ in range(self.FLAPS):
            self._flap_once(mixer, source, scheduler)
            assert len(gst_list(mixer.audiomixer.iterate_sink_pads())) == base_pads, \
                'an audiomixer pad was left behind by a reconnect'

    def test_pipeline_element_count_is_stable(self, mixer, source, scheduler):
        self._flap_once(mixer, source, scheduler)
        settled = len(gst_list(mixer.pipeline.iterate_elements()))
        for _ in range(self.FLAPS):
            self._flap_once(mixer, source, scheduler)
        assert len(gst_list(mixer.pipeline.iterate_elements())) == settled, \
            'elements are accumulating in the pipeline across reconnects'

    def test_only_one_retry_is_ever_pending(self, mixer, source, scheduler):
        """Overlapping timers would compound into a storm of attempts."""
        for _ in range(self.FLAPS):
            simulate_stream_arriving(mixer, source)
            before = len(scheduler.timers)
            source.handle_disconnect('flap')
            # A second disconnect before the retry fires must not double-arm.
            source.handle_disconnect('flap again')
            assert len(scheduler.timers) == before + 1
            scheduler.fire_last()

    def test_backoff_restarts_after_each_successful_connection(
            self, mixer, source, scheduler):
        """A flap is a fresh outage, not a continuation of the last one.

        Backing off further every time would mean a source that recovers
        cleanly each cycle drifts towards 30s of black for no reason.
        """
        for _ in range(5):
            simulate_stream_arriving(mixer, source)
            assert source.reconnect_attempts == 0
            source.handle_disconnect('flap')
            low, high = expected_range(0)
            assert low <= scheduler.delays[-1] <= high
            scheduler.fire_last()

    def test_source_stays_usable_after_sustained_flapping(
            self, mixer, source, scheduler):
        for _ in range(self.FLAPS):
            self._flap_once(mixer, source, scheduler)
        simulate_stream_arriving(mixer, source)
        assert source.state == rtmpsource.CONNECTED
        assert source.reconnect_attempts == 0
        assert source.last_error is None
        assert source.elements, 'source ended up with no elements'
        assert source.get_info()['video']['xpos'] == 10

    def test_removal_mid_flap_leaves_nothing_behind(
            self, mixer, source, scheduler):
        # Measured with the source down, so this is the base layers alone.
        base_video = len(gst_list(mixer.compositor.iterate_sink_pads()))
        base_audio = len(gst_list(mixer.audiomixer.iterate_sink_pads()))
        for _ in range(5):
            self._flap_once(mixer, source, scheduler)

        simulate_stream_arriving(mixer, source)
        assert len(gst_list(mixer.compositor.iterate_sink_pads())) == base_video + 1
        assert len(gst_list(mixer.audiomixer.iterate_sink_pads())) == base_audio + 1

        source.remove()
        assert len(gst_list(mixer.compositor.iterate_sink_pads())) == base_video
        assert len(gst_list(mixer.audiomixer.iterate_sink_pads())) == base_audio
        assert source.elements == []


class TestEosProbe:
    """EOS on the source pad is the only signal a publisher has gone."""

    def _eos_probe_info(self):
        class Info:
            def get_event(self):
                return Gst.Event.new_eos()
        return Info()

    def _flush_probe_info(self):
        class Info:
            def get_event(self):
                return Gst.Event.new_flush_start()
        return Info()

    def test_eos_triggers_a_reconnect_and_is_dropped(self, source, scheduler):
        result = source._on_src_event(None, self._eos_probe_info())
        # Dropped so it never reaches the mixers and marks their pads done.
        assert result == Gst.PadProbeReturn.DROP
        assert source.state == rtmpsource.RECONNECTING
        low, high = expected_range(0)
        assert low <= scheduler.delays[0] <= high

    def test_other_events_pass_through_untouched(self, source, scheduler):
        result = source._on_src_event(None, self._flush_probe_info())
        assert result == Gst.PadProbeReturn.OK
        assert source.state == rtmpsource.CONNECTING
        assert scheduler.timers == []


class TestBusRouting:
    """An error from a source's own elements must reconnect that source."""

    def test_error_is_routed_to_the_owning_source(self, mixer, source):
        assert mixer._source_for(source.rtmp_src) is source

    def test_error_from_a_nested_element_finds_the_source(self, mixer, source):
        """Errors usually surface from inside a decodebin, not the top level."""
        nested = Gst.ElementFactory.make('identity', None)
        source.elements.append(nested)
        assert mixer._source_for(nested) is source

    def test_mixer_elements_are_not_attributed_to_a_source(self, mixer, source):
        assert mixer._source_for(mixer.x264enc) is None
        assert mixer._source_for(mixer.flvmux) is None
