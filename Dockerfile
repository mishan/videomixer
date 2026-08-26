# Single-stage on purpose: the apt layer is the expensive part and Docker
# caches it on its own, so the old videomixer-base image bought nothing except
# a prerequisite step that had to be run by hand before anything else worked.
FROM debian:trixie-slim

LABEL org.opencontainers.image.title="videomixer" \
      org.opencontainers.image.description="RTMP mixing middleware built on GStreamer" \
      org.opencontainers.image.source="https://github.com/mishan/videomixer"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# python3-gi has to come from apt so it matches the system GObject
# introspection typelibs; installing PyGObject from pip means compiling it
# against mismatched headers.
#
# Element -> package, for when something goes missing:
#   compositor, audiomixer, decodebin, videoconvert, audioconvert  -> plugins-base
#   flvmux, flvdemux, aacparse                                     -> plugins-good
#   rtmp2src, rtmp2sink, h264parse                                 -> plugins-bad
#   x264enc                                                        -> plugins-ugly
#   avenc_aac                                                      -> libav
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-gi \
      python3-venv \
      gir1.2-gstreamer-1.0 \
      gir1.2-gst-plugins-base-1.0 \
      gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly \
      gstreamer1.0-libav \
      gstreamer1.0-tools \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Debian marks the system Python as externally managed (PEP 668), so pip
# installs go into a venv. --system-site-packages keeps the apt-installed gi
# visible inside it.
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv --system-site-packages "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /videomixer

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/health', timeout=3)" || exit 1

CMD ["python3", "mix.py"]
