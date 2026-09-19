#!/usr/bin/env python3
"""HTTP control plane for the mixer.

Handlers are plain async coroutines on modern aiohttp. The original versions
were `def` functions using `yield from`, which was aiohttp 2.x style and
silently returns a generator object on aiohttp 3.x -- every endpoint was dead.
"""

import logging

from aiohttp import web

import layout
import videomixer

log = logging.getLogger(__name__)


def _error(message, status=400):
    return web.json_response({'status': 'FAIL', 'error': message},
                             status=status)


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
            raise web.HTTPNotFound(
                text='{{"status": "FAIL", "error": "no such stream {}"}}'
                     .format(stream_id),
                content_type='application/json')
        return stream_id, self.videomixers[stream_id]

    @staticmethod
    async def _body(request):
        try:
            return await request.json()
        except ValueError:
            raise web.HTTPBadRequest(
                text='{"status": "FAIL", "error": "invalid JSON body"}',
                content_type='application/json')

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

        try:
            mixer = videomixer.VideoMixer(
                output_uri,
                width=int(body.get('width', 1280)),
                height=int(body.get('height', 720)),
                fps=int(body.get('fps', 30)),
                video_bitrate=int(body.get('video_bitrate', 2500)),
                audio_bitrate=int(body.get('audio_bitrate', 128)))
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
        alpha = float(body.get('alpha', 1.0))
        if not 0 <= alpha <= 1:
            return _error('alpha must be between 0 and 1')

        try:
            mixer.add_rtmp_source(
                source_id,
                body['stream_uri'],
                xpos=int(body.get('x', 0)),
                ypos=int(body.get('y', 0)),
                zorder=int(body.get('z', 1)),
                width=int(body['width']) if body.get('width') else None,
                height=int(body['height']) if body.get('height') else None,
                fit=fit,
                alpha=alpha)
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

        try:
            mixer.resize_rtmp_source(source_id, int(body['width']),
                                     int(body['height']))
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

        try:
            mixer.move_rtmp_source(
                source_id,
                int(body.get('x', source.xpos)),
                int(body.get('y', source.ypos)),
                int(body.get('z', source.zorder)))
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

        try:
            mixer.set_source_alpha(source_id, float(body['alpha']))
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
