"""HTTP API tests.

These run against a fake mixer rather than a real pipeline: the goal is
routing, validation, status codes and that handlers actually call through to
the methods they claim to. Pipeline behaviour is covered in test_pipeline.py.
"""

import inspect

import pytest
from aiohttp.test_utils import TestClient, TestServer

import layout
import mixerapi


class FakeSource:
    def __init__(self, xpos=0, ypos=0, zorder=1):
        self.xpos, self.ypos, self.zorder = xpos, ypos, zorder

    def get_info(self):
        return {'xpos': self.xpos, 'ypos': self.ypos, 'zorder': self.zorder}


class FakeMixer:
    """Records calls so tests can assert the handler reached the right method."""

    instances = []

    # Real geometry, because the layout module the handlers hand specs to is
    # the real one: only the pipeline is faked.
    WIDTH, HEIGHT = 1280, 720

    def __init__(self, output_url, **kwargs):
        self.output_url = output_url
        self.kwargs = kwargs
        self.width = kwargs.get('width', self.WIDTH)
        self.height = kwargs.get('height', self.HEIGHT)
        self.sources = {}
        self.layout = None
        self.calls = []
        self.played = 0
        self.is_shutdown = False
        FakeMixer.instances.append(self)

    def add_rtmp_source(self, source_id, location, xpos=0, ypos=0, zorder=1,
                        width=None, height=None, fit=layout.CONTAIN,
                        alpha=1.0):
        if source_id in self.sources:
            raise ValueError('source_id={} already exists'.format(source_id))
        self.calls.append(('add', source_id, location, xpos, ypos, zorder,
                           width, height, fit, alpha))
        self.sources[source_id] = FakeSource(xpos, ypos, zorder)
        self.apply_layout()

    def remove_rtmp_source(self, source_id):
        self._require(source_id)
        self.calls.append(('remove', source_id))
        del self.sources[source_id]
        self.apply_layout()

    def set_layout(self, spec):
        cells = self._resolve(spec)
        self.layout = spec
        self.calls.append(('layout', spec))
        return cells

    def apply_layout(self):
        if self.layout is None:
            return {}
        return self._resolve(self.layout)

    def clear_layout(self):
        self.layout = None
        self.calls.append(('clear-layout',))

    def resolved_layout(self):
        return self.apply_layout()

    def _resolve(self, spec):
        return layout.resolve(spec, self.width, self.height,
                              list(self.sources))

    def resize_rtmp_source(self, source_id, width, height):
        self._require(source_id)
        self.calls.append(('resize', source_id, width, height))

    def move_rtmp_source(self, source_id, xpos, ypos, zorder):
        self._require(source_id)
        self.calls.append(('move', source_id, xpos, ypos, zorder))

    def set_source_fit(self, source_id, fit):
        self._require(source_id)
        self.calls.append(('fit', source_id, fit))
        self.layout = None

    def set_source_alpha(self, source_id, alpha):
        self._require(source_id)
        if not 0 <= alpha <= 1:
            raise ValueError('alpha must be between 0 and 1')
        self.calls.append(('alpha', source_id, alpha))
        self.layout = None

    def _require(self, source_id):
        if source_id not in self.sources:
            raise KeyError('source_id={} does not exist'.format(source_id))

    def play(self):
        self.played += 1

    def shutdown(self):
        self.is_shutdown = True

    def get_info(self):
        return {'output_uri': self.output_url,
                'sources': {k: v.get_info() for k, v in self.sources.items()}}


@pytest.fixture
def api(monkeypatch):
    FakeMixer.instances = []
    monkeypatch.setattr(mixerapi.videomixer, 'VideoMixer', FakeMixer)
    return mixerapi.MixerApi()


@pytest.fixture
async def client(api):
    async with TestClient(TestServer(api.app)) as c:
        yield c


async def create_stream(client, stream_id='s1', **overrides):
    body = {'output_uri': 'rtmp://out/live/mixed'}
    body.update(overrides)
    return await client.put('/stream/{}'.format(stream_id), json=body)


# --- the regression that left every endpoint dead -------------------------

def test_every_handler_is_a_coroutine(api):
    """Handlers were `def` using `yield from`, aiohttp 2.x style.

    On aiohttp 3.x those return a generator object instead of a response and
    every endpoint silently 500s, which is how the whole API stayed broken.
    """
    handlers = [r.handler for r in api.app.router.routes()]
    assert handlers, 'no routes registered'
    for handler in handlers:
        assert inspect.iscoroutinefunction(handler), (
            '{} is not a coroutine'.format(handler.__qualname__))


# --- basics ---------------------------------------------------------------

async def test_health(client):
    resp = await client.get('/health')
    assert resp.status == 200
    assert (await resp.json()) == {'status': 'OK', 'streams': 0}


async def test_streams_starts_empty_then_lists(client):
    assert (await (await client.get('/streams')).json()) == []
    await create_stream(client, 'a')
    await create_stream(client, 'b')
    assert (await (await client.get('/streams')).json()) == ['a', 'b']


async def test_unknown_stream_is_404_not_200(client):
    """The old code returned 200 with a FAIL body for missing streams."""
    for method, path in [('get', '/stream/nope'),
                         ('delete', '/stream/nope'),
                         ('put', '/stream/nope/cam'),
                         ('post', '/stream/nope/move/cam'),
                         ('post', '/stream/nope/resize/cam')]:
        resp = await getattr(client, method)(path, json={})
        assert resp.status == 404, '{} {}'.format(method, path)
        assert (await resp.json())['status'] == 'FAIL'


# --- creating streams -----------------------------------------------------

async def test_create_requires_output_uri(client):
    resp = await client.put('/stream/s1', json={'bg_uri': 'rtmp://in/live/x'})
    assert resp.status == 400
    assert 'output_uri' in (await resp.json())['error']


async def test_create_rejects_invalid_json(client):
    resp = await client.put('/stream/s1', data='not json',
                            headers={'Content-Type': 'application/json'})
    assert resp.status == 400


async def test_create_passes_geometry_through(client):
    await create_stream(client, 's1', width=640, height=360, fps=25,
                        video_bitrate=1200, audio_bitrate=96)
    mixer = FakeMixer.instances[-1]
    assert mixer.output_url == 'rtmp://out/live/mixed'
    assert mixer.kwargs == {'width': 640, 'height': 360, 'fps': 25,
                            'video_bitrate': 1200, 'audio_bitrate': 96}
    assert mixer.played == 1


async def test_create_without_bg_uri_starts_empty(client):
    """bg_uri is optional; the base layers keep the output alive."""
    resp = await create_stream(client, 's1')
    assert resp.status == 200
    assert FakeMixer.instances[-1].sources == {}


async def test_create_with_bg_uri_adds_background_at_zorder_zero(client):
    await create_stream(client, 's1', bg_uri='rtmp://in/live/bg')
    assert FakeMixer.instances[-1].calls[0][:3] == ('add', 'bg',
                                                    'rtmp://in/live/bg')
    assert FakeMixer.instances[-1].calls[0][5] == 0  # zorder


async def test_duplicate_stream_is_409(client):
    await create_stream(client, 's1')
    resp = await create_stream(client, 's1')
    assert resp.status == 409


# --- sources --------------------------------------------------------------

async def test_add_source_requires_stream_uri(client):
    await create_stream(client, 's1')
    resp = await client.put('/stream/s1/cam', json={'x': 1})
    assert resp.status == 400
    assert 'stream_uri' in (await resp.json())['error']


async def test_add_source_applies_defaults_and_overrides(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/cam'})
    assert FakeMixer.instances[-1].calls[-1] == (
        'add', 'cam', 'rtmp://in/live/cam', 0, 0, 1, None, None, 'contain', 1.0)

    await client.put('/stream/s1/cam2', json={'stream_uri': 'rtmp://in/live/c2',
                                              'x': 10, 'y': 20, 'z': 5,
                                              'width': 320, 'height': 180})
    assert FakeMixer.instances[-1].calls[-1] == (
        'add', 'cam2', 'rtmp://in/live/c2', 10, 20, 5, 320, 180, 'contain', 1.0)


async def test_add_duplicate_source_is_409(client):
    await create_stream(client, 's1')
    body = {'stream_uri': 'rtmp://in/live/cam'}
    assert (await client.put('/stream/s1/cam', json=body)).status == 200
    assert (await client.put('/stream/s1/cam', json=body)).status == 409


async def test_remove_source(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/cam'})
    resp = await client.delete('/stream/s1/cam')
    assert resp.status == 200
    assert ('remove', 'cam') in FakeMixer.instances[-1].calls


async def test_remove_unknown_source_is_404(client):
    await create_stream(client, 's1')
    assert (await client.delete('/stream/s1/ghost')).status == 404


# --- resize / move: these used to call methods that did not exist ---------

async def test_resize_calls_resize_rtmp_source(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/cam'})
    resp = await client.post('/stream/s1/resize/cam',
                             json={'width': 320, 'height': 180})
    assert resp.status == 200
    assert ('resize', 'cam', 320, 180) in FakeMixer.instances[-1].calls


async def test_resize_requires_both_dimensions(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/cam'})
    resp = await client.post('/stream/s1/resize/cam', json={'width': 320})
    assert resp.status == 400


async def test_move_calls_move_rtmp_source(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/cam'})
    resp = await client.post('/stream/s1/move/cam',
                             json={'x': 40, 'y': 50, 'z': 9})
    assert resp.status == 200
    assert ('move', 'cam', 40, 50, 9) in FakeMixer.instances[-1].calls


async def test_move_keeps_unspecified_axes(client):
    """The old handler referenced undefined names here and always blew up."""
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam',
                     json={'stream_uri': 'rtmp://in/live/cam',
                           'x': 1, 'y': 2, 'z': 3})
    resp = await client.post('/stream/s1/move/cam', json={'x': 99})
    assert resp.status == 200
    assert ('move', 'cam', 99, 2, 3) in FakeMixer.instances[-1].calls


async def test_move_unknown_source_is_404(client):
    await create_stream(client, 's1')
    assert (await client.post('/stream/s1/move/ghost', json={'x': 1})).status == 404


# --- teardown -------------------------------------------------------------

async def test_delete_stream_shuts_the_pipeline_down(client):
    await create_stream(client, 's1')
    mixer = FakeMixer.instances[-1]
    resp = await client.delete('/stream/s1')
    assert resp.status == 200
    assert mixer.is_shutdown
    assert (await (await client.get('/streams')).json()) == []


async def test_app_shutdown_tears_down_every_stream(api):
    async with TestClient(TestServer(api.app)) as c:
        await create_stream(c, 's1')
        await create_stream(c, 's2')
        mixers = list(FakeMixer.instances)
    assert len(mixers) == 2
    assert all(m.is_shutdown for m in mixers)


# --- layout ---------------------------------------------------------------

async def test_layout_route_is_not_mistaken_for_a_source(client):
    """/stream/s1/layout matches the {source_id} pattern too.

    aiohttp resolves routes in registration order, so if the layout routes were
    registered after the source ones, setting a layout would create a source
    called "layout" instead.
    """
    await create_stream(client, 's1')
    resp = await client.put('/stream/s1/layout', json={'preset': 'grid'})
    assert resp.status == 200
    mixer = FakeMixer.instances[-1]
    assert 'layout' not in mixer.sources
    assert mixer.layout == {'preset': 'grid'}


async def test_set_layout_returns_the_resolved_cells(client):
    await create_stream(client, 's1')
    for name in ('a', 'b'):
        await client.put('/stream/s1/' + name,
                         json={'stream_uri': 'rtmp://in/live/' + name})
    resp = await client.put('/stream/s1/layout', json={'preset': 'row'})
    body = await resp.json()
    assert body['status'] == 'OK'
    assert body['cells']['a']['width'] == 640
    assert body['cells']['b']['x'] == 640


async def test_get_layout_reports_the_spec_and_the_cells(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/a', json={'stream_uri': 'rtmp://in/live/a'})
    await client.put('/stream/s1/layout', json={'preset': 'solo'})
    body = await (await client.get('/stream/s1/layout')).json()
    assert body['layout'] == {'preset': 'solo'}
    assert body['cells']['a'] == {'x': 0, 'y': 0, 'width': 1280,
                                  'height': 720, 'z': 1, 'fit': 'contain',
                                  'alpha': 1.0}


async def test_get_layout_with_none_set(client):
    await create_stream(client, 's1')
    body = await (await client.get('/stream/s1/layout')).json()
    assert body['layout'] is None
    assert body['cells'] == {}


async def test_a_bad_layout_is_400_with_a_usable_message(client):
    await create_stream(client, 's1')
    resp = await client.put('/stream/s1/layout', json={'preset': 'mosaic'})
    assert resp.status == 400
    assert 'unknown preset' in (await resp.json())['error']


async def test_a_bad_layout_leaves_the_previous_one_in_force(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/layout', json={'preset': 'grid'})
    await client.put('/stream/s1/layout', json={'preset': 'nonsense'})
    assert FakeMixer.instances[-1].layout == {'preset': 'grid'}


async def test_clear_layout_stops_tracking_without_moving_anything(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/layout', json={'preset': 'grid'})
    resp = await client.delete('/stream/s1/layout')
    assert resp.status == 200
    assert FakeMixer.instances[-1].layout is None


async def test_layout_can_be_set_when_the_stream_is_created(client):
    await create_stream(client, 's1', layout={'preset': 'grid', 'cols': 3},
                        bg_uri='rtmp://in/live/bg')
    mixer = FakeMixer.instances[-1]
    assert mixer.layout == {'preset': 'grid', 'cols': 3}
    # The layout is in force before the background is added, so it lands in
    # place rather than full-frame and then jumping.
    assert [c[0] for c in mixer.calls] == ['layout', 'add']


async def test_a_bad_layout_at_creation_does_not_leave_a_stream_behind(client):
    resp = await create_stream(client, 's1', layout={'preset': 'nope'})
    assert resp.status == 400
    assert 'invalid layout' in (await resp.json())['error']
    assert (await (await client.get('/streams')).json()) == []
    assert FakeMixer.instances[-1].is_shutdown


async def test_layout_endpoints_404_on_an_unknown_stream(client):
    for method in ('get', 'put', 'delete'):
        resp = await getattr(client, method)('/stream/nope/layout', json={})
        assert resp.status == 404


async def test_add_source_accepts_fit_and_alpha(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/c',
                                             'fit': 'fill', 'alpha': 0.5})
    assert FakeMixer.instances[-1].calls[-1][-2:] == ('fill', 0.5)


@pytest.mark.parametrize('body,expected', [
    ({'fit': 'cover'}, 'fit must be one of'),
    ({'alpha': 2}, 'alpha must be between'),
])
async def test_add_source_rejects_bad_fit_and_alpha(client, body, expected):
    await create_stream(client, 's1')
    body = dict(body, stream_uri='rtmp://in/live/c')
    resp = await client.put('/stream/s1/cam', json=body)
    assert resp.status == 400
    assert expected in (await resp.json())['error']


async def test_fit_endpoint_sets_the_fit(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/c'})
    resp = await client.post('/stream/s1/fit/cam', json={'fit': 'fill'})
    assert resp.status == 200
    assert ('fit', 'cam', 'fill') in FakeMixer.instances[-1].calls


async def test_alpha_endpoint_sets_the_alpha(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/c'})
    resp = await client.post('/stream/s1/alpha/cam', json={'alpha': 0.25})
    assert resp.status == 200
    assert ('alpha', 'cam', 0.25) in FakeMixer.instances[-1].calls


@pytest.mark.parametrize('path,body,expected', [
    ('/stream/s1/fit/cam', {}, 'fit is required'),
    ('/stream/s1/fit/cam', {'fit': 'cover'}, 'fit must be one of'),
    ('/stream/s1/alpha/cam', {}, 'alpha is required'),
    ('/stream/s1/alpha/cam', {'alpha': 4}, 'alpha must be between'),
])
async def test_fit_and_alpha_reject_bad_input(client, path, body, expected):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/c'})
    resp = await client.post(path, json=body)
    assert resp.status == 400
    assert expected in (await resp.json())['error']


async def test_fit_and_alpha_on_an_unknown_source_are_404(client):
    await create_stream(client, 's1')
    assert (await client.post('/stream/s1/fit/ghost',
                              json={'fit': 'fill'})).status == 404
    assert (await client.post('/stream/s1/alpha/ghost',
                              json={'alpha': 1})).status == 404


async def test_fit_and_alpha_take_the_layout_out_of_force(client):
    """Cells carry fit and alpha too, so the layout would undo these on its
    next resolve."""
    await create_stream(client, 's1', layout={'preset': 'grid'})
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/c'})
    await client.post('/stream/s1/fit/cam', json={'fit': 'fill'})
    assert FakeMixer.instances[-1].layout is None
