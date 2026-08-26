"""HTTP API tests.

These run against a fake mixer rather than a real pipeline: the goal is
routing, validation, status codes and that handlers actually call through to
the methods they claim to. Pipeline behaviour is covered in test_pipeline.py.
"""

import inspect

import pytest
from aiohttp.test_utils import TestClient, TestServer

import mixerapi


class FakeSource:
    def __init__(self, xpos=0, ypos=0, zorder=1):
        self.xpos, self.ypos, self.zorder = xpos, ypos, zorder

    def get_info(self):
        return {'xpos': self.xpos, 'ypos': self.ypos, 'zorder': self.zorder}


class FakeMixer:
    """Records calls so tests can assert the handler reached the right method."""

    instances = []

    def __init__(self, output_url, **kwargs):
        self.output_url = output_url
        self.kwargs = kwargs
        self.sources = {}
        self.calls = []
        self.played = 0
        self.is_shutdown = False
        FakeMixer.instances.append(self)

    def add_rtmp_source(self, pip_id, location, xpos=0, ypos=0, zorder=1,
                        width=None, height=None):
        if pip_id in self.sources:
            raise ValueError('pip_id={} already exists'.format(pip_id))
        self.calls.append(('add', pip_id, location, xpos, ypos, zorder,
                           width, height))
        self.sources[pip_id] = FakeSource(xpos, ypos, zorder)

    def remove_rtmp_source(self, pip_id):
        self._require(pip_id)
        self.calls.append(('remove', pip_id))
        del self.sources[pip_id]

    def resize_rtmp_source(self, pip_id, width, height):
        self._require(pip_id)
        self.calls.append(('resize', pip_id, width, height))

    def move_rtmp_source(self, pip_id, xpos, ypos, zorder):
        self._require(pip_id)
        self.calls.append(('move', pip_id, xpos, ypos, zorder))

    def _require(self, pip_id):
        if pip_id not in self.sources:
            raise KeyError('pip_id={} does not exist'.format(pip_id))

    def play(self):
        self.played += 1

    def shutdown(self):
        self.is_shutdown = True

    def get_info(self):
        return {'output_uri': self.output_url,
                'pip_streams': {k: v.get_info() for k, v in self.sources.items()}}


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
                         ('put', '/stream/nope/pip'),
                         ('post', '/stream/nope/move/pip'),
                         ('post', '/stream/nope/resize/pip')]:
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


# --- picture-in-picture ---------------------------------------------------

async def test_add_pip_requires_stream_uri(client):
    await create_stream(client, 's1')
    resp = await client.put('/stream/s1/cam', json={'x': 1})
    assert resp.status == 400
    assert 'stream_uri' in (await resp.json())['error']


async def test_add_pip_applies_defaults_and_overrides(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/cam'})
    assert FakeMixer.instances[-1].calls[-1] == (
        'add', 'cam', 'rtmp://in/live/cam', 0, 0, 1, None, None)

    await client.put('/stream/s1/cam2', json={'stream_uri': 'rtmp://in/live/c2',
                                              'x': 10, 'y': 20, 'z': 5,
                                              'width': 320, 'height': 180})
    assert FakeMixer.instances[-1].calls[-1] == (
        'add', 'cam2', 'rtmp://in/live/c2', 10, 20, 5, 320, 180)


async def test_add_duplicate_pip_is_409(client):
    await create_stream(client, 's1')
    body = {'stream_uri': 'rtmp://in/live/cam'}
    assert (await client.put('/stream/s1/cam', json=body)).status == 200
    assert (await client.put('/stream/s1/cam', json=body)).status == 409


async def test_remove_pip(client):
    await create_stream(client, 's1')
    await client.put('/stream/s1/cam', json={'stream_uri': 'rtmp://in/live/cam'})
    resp = await client.delete('/stream/s1/cam')
    assert resp.status == 200
    assert ('remove', 'cam') in FakeMixer.instances[-1].calls


async def test_remove_unknown_pip_is_404(client):
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


async def test_move_unknown_pip_is_404(client):
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
