"""Shared fixtures.

Pipelines are built but never set to PLAYING, so nothing here opens a socket
or needs an RTMP server. rtmp2sink only connects on the transition out of
NULL, which makes full pipeline construction safe to assert on in a unit test.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import gi  # noqa: E402
except ImportError as exc:  # pragma: no cover - environment problem
    raise RuntimeError(
        '\n'
        'PyGObject (gi) is not importable, so the tests cannot run.\n'
        '\n'
        'It is installed from your distro rather than pip, because it has to\n'
        'match the system GObject introspection typelibs:\n'
        '\n'
        '    sudo apt install python3-gi gir1.2-gstreamer-1.0 \\\n'
        '        gstreamer1.0-plugins-{base,good,bad,ugly} gstreamer1.0-libav\n'
        '\n'
        'A virtualenv hides system packages unless it is told not to, which is\n'
        'the usual cause of this. Recreate yours with --system-site-packages:\n'
        '\n'
        '    python3 -m venv --system-site-packages ~/.virtualenvs/videomixer\n'
        '    ~/.virtualenvs/videomixer/bin/pip install -r requirements-dev.txt\n'
        '\n'
        'Or run the suite in the container, which already has everything:\n'
        '\n'
        '    make unit-docker\n'
    ) from exc

gi.require_version('Gst', '1.0')
from gi.repository import Gst  # noqa: E402

Gst.init(None)

# Somewhere nothing is listening: these pipelines never leave NULL, but if a
# regression ever starts them, it should fail fast rather than hit a server.
DEAD_RTMP_URL = 'rtmp://127.0.0.1:1/nonexistent/nowhere'


@pytest.fixture
def mixer():
    """A fully constructed VideoMixer, torn down afterwards."""
    import videomixer
    m = videomixer.VideoMixer(DEAD_RTMP_URL, width=320, height=180, fps=15)
    yield m
    m.shutdown()


def gst_list(iterator):
    """Drain a GstIterator into a list.

    PyGObject does not expose GstIterator as a Python iterable, so this walks
    it by hand and restarts on the resync GStreamer raises when the underlying
    collection changes mid-iteration.
    """
    items = []
    while True:
        result, value = iterator.next()
        if result == Gst.IteratorResult.DONE:
            return items
        if result == Gst.IteratorResult.RESYNC:
            iterator.resync()
            items = []
            continue
        if result == Gst.IteratorResult.ERROR:
            raise RuntimeError('error iterating GstIterator')
        items.append(value)


@pytest.fixture
def element_names(mixer):
    """Factory names of every element in the mixer's pipeline."""
    return {e.get_factory().get_name()
            for e in gst_list(mixer.pipeline.iterate_elements())}
