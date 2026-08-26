#!/usr/bin/env python3
"""videomixer entrypoint.

GStreamer needs a GLib main loop running for bus watches and pad-added
callbacks to fire; aiohttp needs an asyncio loop. The old code tried to fuse
the two with gbulb (and later asyncio_glib), both of which are unmaintained
and neither of which works on current Python.

They do not actually need to be the same loop. GStreamer is thread-safe, so
the GLib loop runs on a daemon thread and aiohttp owns the main thread. That
removes both dependencies and the whole class of event-loop-policy problems.
"""

import argparse
import logging
import os
import sys
import threading

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib  # noqa: E402

from aiohttp import web  # noqa: E402

import mixerapi  # noqa: E402

log = logging.getLogger('videomixer')


class GLibLoopThread:
    """Runs a GLib main loop on a background daemon thread."""

    def __init__(self):
        self.loop = GLib.MainLoop()
        self.thread = threading.Thread(target=self.loop.run,
                                       name='glib-mainloop',
                                       daemon=True)

    def start(self):
        self.thread.start()
        log.debug('GLib main loop started on background thread')

    def stop(self):
        if self.loop.is_running():
            self.loop.quit()


def parse_args(argv):
    parser = argparse.ArgumentParser(description='RTMP video mixer')
    parser.add_argument('--bind', default=os.environ.get('MIX_BIND', '0.0.0.0'),
                        help='address to bind the HTTP API to')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('MIX_PORT', 8888)),
                        help='port for the HTTP API')
    parser.add_argument('--log-level',
                        default=os.environ.get('MIX_LOG_LEVEL', 'INFO'),
                        help='DEBUG, INFO, WARNING or ERROR')
    parser.add_argument('--gst-debug', type=int,
                        default=int(os.environ.get('GST_DEBUG_LEVEL', 0)),
                        help='GStreamer debug threshold (0 disables)')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')

    Gst.init(None)
    if args.gst_debug:
        Gst.debug_set_active(True)
        Gst.debug_set_default_threshold(args.gst_debug)
    log.info('GStreamer %s', Gst.version_string())

    glib_loop = GLibLoopThread()
    glib_loop.start()

    api = mixerapi.MixerApi()
    log.info('Listening on %s:%s', args.bind, args.port)
    try:
        web.run_app(api.app, host=args.bind, port=args.port,
                    print=None, access_log=None)
    finally:
        glib_loop.stop()
        log.info('Shut down')


if __name__ == '__main__':
    main()
