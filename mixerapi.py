#!/usr/bin/env python3
"""HTTP control plane for the mixer.

Handlers are plain async coroutines on modern aiohttp. The original versions
were `def` functions using `yield from`, which was aiohttp 2.x style and
silently returns a generator object on aiohttp 3.x -- every endpoint was dead.
"""

import logging

from aiohttp import web

import videomixer

log = logging.getLogger(__name__)


def _error(message, status=400):
    return web.json_response({'status': 'FAIL', 'error': message},
                             status=status)


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
            web.put('/stream/{stream_id}/{pip_id}', self.add_stream_handler),
            web.delete('/stream/{stream_id}/{pip_id}',
                       self.remove_pip_handler),
            web.post('/stream/{stream_id}/resize/{pip_id}',
                     self.resize_handler),
            web.post('/stream/{stream_id}/move/{pip_id}',
                     self.move_pip_handler),
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
            # bg_uri is optional now: the mixer has its own black/silent base
            # layer, so a stream can start empty and have sources added later.
            if bg_uri:
                mixer.add_rtmp_source('bg', bg_uri, zorder=0)
            mixer.play()
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

    async def add_stream_handler(self, request):
        stream_id, mixer = self._mixer(request)
        pip_id = request.match_info['pip_id']

        body = await self._body(request)
        if 'stream_uri' not in body:
            return _error('stream_uri is required')

        try:
            mixer.add_rtmp_source(
                pip_id,
                body['stream_uri'],
                xpos=int(body.get('x', 0)),
                ypos=int(body.get('y', 0)),
                zorder=int(body.get('z', 1)),
                width=int(body['width']) if body.get('width') else None,
                height=int(body['height']) if body.get('height') else None)
        except ValueError as exc:
            return _error(str(exc), 409)
        except Exception as exc:
            log.exception('Failed to add pip %s to %s', pip_id, stream_id)
            return _error('could not add source: {}'.format(exc), 500)

        # New elements are synced to the running pipeline as they are added,
        # but re-asserting PLAYING is cheap and covers a paused mixer.
        mixer.play()
        return _ok(stream_id=stream_id, pip_id=pip_id)

    async def remove_pip_handler(self, request):
        stream_id, mixer = self._mixer(request)
        pip_id = request.match_info['pip_id']
        try:
            mixer.remove_rtmp_source(pip_id)
        except KeyError as exc:
            return _error(str(exc), 404)
        return _ok(stream_id=stream_id, pip_id=pip_id)

    async def resize_handler(self, request):
        stream_id, mixer = self._mixer(request)
        pip_id = request.match_info['pip_id']

        body = await self._body(request)
        if 'width' not in body or 'height' not in body:
            return _error('width and height are required')

        try:
            mixer.resize_rtmp_source(pip_id, int(body['width']),
                                     int(body['height']))
        except KeyError as exc:
            return _error(str(exc), 404)
        return _ok(stream_id=stream_id, pip_id=pip_id)

    async def move_pip_handler(self, request):
        stream_id, mixer = self._mixer(request)
        pip_id = request.match_info['pip_id']

        body = await self._body(request)
        try:
            source = mixer.sources[pip_id]
        except KeyError:
            return _error('pip_id={} does not exist'.format(pip_id), 404)

        try:
            mixer.move_rtmp_source(
                pip_id,
                int(body.get('x', source.xpos)),
                int(body.get('y', source.ypos)),
                int(body.get('z', source.zorder)))
        except KeyError as exc:
            return _error(str(exc), 404)
        return _ok(stream_id=stream_id, pip_id=pip_id)

    # -- lifecycle ---------------------------------------------------------

    async def _on_shutdown(self, app):
        for stream_id, mixer in list(self.videomixers.items()):
            log.info('Tearing down stream %s', stream_id)
            mixer.shutdown()
        self.videomixers.clear()
