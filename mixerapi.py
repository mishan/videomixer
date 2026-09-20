#!/usr/bin/env python3
"""HTTP control plane for the mixer.

Handlers are plain async coroutines on modern aiohttp. The original versions
were `def` functions using `yield from`, which was aiohttp 2.x style and
silently returns a generator object on aiohttp 3.x -- every endpoint was dead.
"""

import json
import logging

from aiohttp import web

import layout
import videomixer

log = logging.getLogger(__name__)


def _error(message, status=400):
    return web.json_response({'status': 'FAIL', 'error': message},
                             status=status)


_ABORTS = {400: web.HTTPBadRequest, 404: web.HTTPNotFound,
           409: web.HTTPConflict}


def _abort(message, status=400):
    """Raise a JSON error from somewhere a return is not available.

    The coercion helpers below are called inline in argument lists, where
    returning a response is not an option. json.dumps rather than a format
    string because the message can contain a caller-supplied value -- a stream
    id with a quote in it would otherwise produce a body that is not JSON.
    """
    raise _ABORTS[status](
        text=json.dumps({'status': 'FAIL', 'error': message}),
        content_type='application/json')


def _number(body, key, default, cast, noun):
    """Read a numeric field, or fail the request with a 400.

    Bare int()/float() on request data turns a typo into an HTTP 500 and a
    stack trace in the log: int("wide") raises ValueError and a JSON null
    raises TypeError. Both are the client's mistake, not the server's.

    A field that is absent falls back to the default; a field that is present
    but null does not. Sending {"alpha": null} is a malformed request, not a
    request for the default -- treating it as the latter is how a null slipped
    past the range check and became a 500 of its own.
    """
    if key not in body:
        return default
    try:
        return cast(body[key])
    except (TypeError, ValueError):
        _abort('{} must be {}, not {}'.format(key, noun, json.dumps(body[key])))


def _int(body, key, default=None):
    return _number(body, key, default, int, 'a whole number')


def _float(body, key, default=None):
    return _number(body, key, default, float, 'a number')


def _cells(cells):
    return {source_id: cell.as_dict() for source_id, cell in cells.items()}


def _ok(**extra):
    payload = {'status': 'OK'}
    payload.update(extra)
    return web.json_response(payload)


class MixerApi:
    def __init__(self):
        self.videomixers = {}
        self.app = web.Application()
        self.app.add_routes([
            web.get('/health', self.health_handler),
            web.get('/streams', self.get_streams_handler),
            web.get('/stream/{stream_id}', self.get_stream_handler),
            web.put('/stream/{stream_id}', self.create_handler),
            web.delete('/stream/{stream_id}', self.delete_handler),
            # The layout routes have to be registered before the
            # {source_id} ones: aiohttp matches in registration order and
            # /stream/x/layout fits both patterns, so the other way round a
            # layout request would create a source called "layout".
            web.get('/stream/{stream_id}/layout', self.get_layout_handler),
            web.put('/stream/{stream_id}/layout', self.set_layout_handler),
            web.delete('/stream/{stream_id}/layout',
                       self.clear_layout_handler),
            web.put('/stream/{stream_id}/{source_id}', self.add_source_handler),
            web.delete('/stream/{stream_id}/{source_id}',
                       self.remove_source_handler),
            web.post('/stream/{stream_id}/resize/{source_id}',
                     self.resize_source_handler),
            web.post('/stream/{stream_id}/move/{source_id}',
                     self.move_source_handler),
            web.post('/stream/{stream_id}/fit/{source_id}',
                     self.fit_handler),
            web.post('/stream/{stream_id}/alpha/{source_id}',
                     self.alpha_handler),
        ])
        self.app.on_shutdown.append(self._on_shutdown)

    # -- helpers -----------------------------------------------------------

    def _mixer(self, request):
        """Look up the mixer for this request, or raise a 404."""
        stream_id = request.match_info['stream_id']
        if stream_id not in self.videomixers:
            _abort('no such stream {}'.format(stream_id), 404)
        return stream_id, self.videomixers[stream_id]

    @staticmethod
    async def _body(request):
        try:
            return await request.json()
        except ValueError:
            _abort('invalid JSON body')

    # -- handlers ----------------------------------------------------------

    async def health_handler(self, request):
        return _ok(streams=len(self.videomixers))

    async def get_streams_handler(self, request):
        return web.json_response(sorted(self.videomixers))

    async def get_stream_handler(self, request):
        stream_id, mixer = self._mixer(request)
        return web.json_response({'stream_id': stream_id,
                                  'mixer': mixer.get_info()})

    async def create_handler(self, request):
        stream_id = request.match_info['stream_id']
        if stream_id in self.videomixers:
            return _error('stream {} already exists'.format(stream_id), 409)

        body = await self._body(request)
        if 'output_uri' not in body:
            return _error('output_uri is required')

        output_uri = body['output_uri']
        bg_uri = body.get('bg_uri')
        log.info('Creating stream %s -> %s', stream_id, output_uri)

        # Coerced before the try: _abort raises, and the blanket except below
        # would turn its 400 into a 500.
        geometry = dict(
            width=_int(body, 'width', 1280),
            height=_int(body, 'height', 720),
            fps=_int(body, 'fps', 30),
            video_bitrate=_int(body, 'video_bitrate', 2500),
            audio_bitrate=_int(body, 'audio_bitrate', 128))

        try:
            mixer = videomixer.VideoMixer(output_uri, **geometry)
            # Set before the background is added so the source lands in the
            # layout rather than full-frame and then jumping.
            if 'layout' in body:
                mixer.set_layout(body['layout'])
            # bg_uri is optional now: the mixer has its own black/silent base
            # layer, so a stream can start empty and have sources added later.
            if bg_uri:
                mixer.add_rtmp_source('bg', bg_uri, zorder=0)
            mixer.play()
        except layout.LayoutError as exc:
            mixer.shutdown()
            return _error('invalid layout: {}'.format(exc))
        except Exception as exc:
            log.exception('Failed to create stream %s', stream_id)
            return _error('could not create stream: {}'.format(exc), 500)

        self.videomixers[stream_id] = mixer
        return _ok(stream_id=stream_id)

    async def delete_handler(self, request):
        stream_id, mixer = self._mixer(request)
        mixer.shutdown()
        del self.videomixers[stream_id]
        log.info('Deleted stream %s', stream_id)
        return _ok(stream_id=stream_id)

    async def add_source_handler(self, request):
        stream_id, mixer = self._mixer(request)
        source_id = request.match_info['source_id']

        body = await self._body(request)
        if 'stream_uri' not in body:
            return _error('stream_uri is required')

        fit = body.get('fit', layout.CONTAIN)
        if fit not in layout.FITS:
            return _error('fit must be one of {}'.format(
                ', '.join(layout.FITS)))
        alpha = _float(body, 'alpha', 1.0)
        if not 0 <= alpha <= 1:
            return _error('alpha must be between 0 and 1')
        placement = dict(
            xpos=_int(body, 'x', 0),
            ypos=_int(body, 'y', 0),
            zorder=_int(body, 'z', 1),
            # 0 means "native size" to the compositor, same as absent.
            width=_int(body, 'width') or None,
            height=_int(body, 'height') or None)

        try:
            mixer.add_rtmp_source(source_id, body['stream_uri'],
                                  fit=fit, alpha=alpha, **placement)
        except ValueError as exc:
            return _error(str(exc), 409)
        except Exception as exc:
            log.exception('Failed to add source %s to %s', source_id, stream_id)
            return _error('could not add source: {}'.format(exc), 500)

        # New elements are synced to the running pipeline as they are added,
        # but re-asserting PLAYING is cheap and covers a paused mixer.
        mixer.play()
        return _ok(stream_id=stream_id, source_id=source_id)

    async def remove_source_handler(self, request):
        stream_id, mixer = self._mixer(request)
        source_id = request.match_info['source_id']
        try:
            mixer.remove_rtmp_source(source_id)
        except KeyError as exc:
            return _error(str(exc), 404)
        return _ok(stream_id=stream_id, source_id=source_id)

    async def resize_source_handler(self, request):
        stream_id, mixer = self._mixer(request)
        source_id = request.match_info['source_id']

        body = await self._body(request)
        if 'width' not in body or 'height' not in body:
            return _error('width and height are required')

        width, height = _int(body, 'width'), _int(body, 'height')
        try:
            mixer.resize_rtmp_source(source_id, width, height)
        except KeyError as exc:
            return _error(str(exc), 404)
        return _ok(stream_id=stream_id, source_id=source_id)

    async def move_source_handler(self, request):
        stream_id, mixer = self._mixer(request)
        source_id = request.match_info['source_id']

        body = await self._body(request)
        try:
            source = mixer.sources[source_id]
        except KeyError:
            return _error('source_id={} does not exist'.format(source_id), 404)

        xpos = _int(body, 'x', source.xpos)
        ypos = _int(body, 'y', source.ypos)
        zorder = _int(body, 'z', source.zorder)
        try:
            mixer.move_rtmp_source(source_id, xpos, ypos, zorder)
        except KeyError as exc:
            return _error(str(exc), 404)
        return _ok(stream_id=stream_id, source_id=source_id)

    async def fit_handler(self, request):
        stream_id, mixer = self._mixer(request)
        source_id = request.match_info['source_id']

        body = await self._body(request)
        if 'fit' not in body:
            return _error('fit is required')
        if body['fit'] not in layout.FITS:
            return _error('fit must be one of {}'.format(
                ', '.join(layout.FITS)))

        try:
            mixer.set_source_fit(source_id, body['fit'])
        except KeyError as exc:
            return _error(str(exc), 404)
        return _ok(stream_id=stream_id, source_id=source_id)

    async def alpha_handler(self, request):
        stream_id, mixer = self._mixer(request)
        source_id = request.match_info['source_id']

        body = await self._body(request)
        if 'alpha' not in body:
            return _error('alpha is required')
        alpha = _float(body, 'alpha')

        try:
            mixer.set_source_alpha(source_id, alpha)
        except KeyError as exc:
            return _error(str(exc), 404)
        except ValueError as exc:
            return _error(str(exc))
        return _ok(stream_id=stream_id, source_id=source_id)

    # -- layout ------------------------------------------------------------

    async def get_layout_handler(self, request):
        stream_id, mixer = self._mixer(request)
        return web.json_response({
            'stream_id': stream_id,
            'layout': mixer.layout,
            'cells': _cells(mixer.resolved_layout()),
        })

    async def set_layout_handler(self, request):
        stream_id, mixer = self._mixer(request)
        body = await self._body(request)
        # This route shadows PUT /stream/{id}/{source_id}, so "layout" is not
        # available as a source id. Say so, rather than letting a request that
        # is plainly trying to add a source fail as an invalid layout spec.
        if 'stream_uri' in body:
            return _error(
                '"layout" is a reserved source id on this stream: '
                'PUT /stream/{}/layout sets the layout. Give the source '
                'another name.'.format(stream_id), 409)
        try:
            cells = mixer.set_layout(body)
        except layout.LayoutError as exc:
            return _error(str(exc))
        return _ok(stream_id=stream_id, layout=body, cells=_cells(cells))

    async def clear_layout_handler(self, request):
        """Stop tracking a layout without moving anything.

        The picture does not change; what changes is that the next source to
        join or drop no longer reshapes it.
        """
        stream_id, mixer = self._mixer(request)
        mixer.clear_layout()
        return _ok(stream_id=stream_id)

    # -- lifecycle ---------------------------------------------------------

    async def _on_shutdown(self, app):
        for stream_id, mixer in list(self.videomixers.items()):
            log.info('Tearing down stream %s', stream_id)
            mixer.shutdown()
        self.videomixers.clear()
