# nginx + nginx-rtmp-module, built from source in a throwaway stage so the
# runtime image carries no compiler or headers.
#
# The module has had no tagged release since 1.2.2 (2019), but master builds
# clean against current nginx, so it is pinned to a commit rather than a tag.
ARG NGINX_VERSION=1.30.4
ARG RTMP_MODULE_COMMIT=6c7719d0ba32e00b563ec70bd43dad11960fa9c4

FROM debian:trixie-slim AS build

ARG NGINX_VERSION
ARG RTMP_MODULE_COMMIT
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl ca-certificates \
      libpcre2-dev zlib1g-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL "https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz" \
      | tar xz \
 && curl -fsSL "https://github.com/arut/nginx-rtmp-module/archive/${RTMP_MODULE_COMMIT}.tar.gz" \
      | tar xz \
 && mv "nginx-rtmp-module-${RTMP_MODULE_COMMIT}" nginx-rtmp-module

RUN cd "nginx-${NGINX_VERSION}" \
 && ./configure \
      --prefix=/usr/local/nginx \
      --add-module=/src/nginx-rtmp-module \
      --with-http_ssl_module \
      --with-threads \
      --error-log-path=/dev/stderr \
      --http-log-path=/dev/stdout \
 && make -j"$(nproc)" \
 && make install \
 && mkdir -p /usr/local/nginx/xsl \
 && cp /src/nginx-rtmp-module/stat.xsl /usr/local/nginx/xsl/


FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH=/usr/local/nginx/sbin:$PATH

# Only the shared libraries nginx actually links against + curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpcre2-8-0 zlib1g libssl3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/nginx /usr/local/nginx
COPY nginx.conf /usr/local/nginx/nginx.conf

# nginx needs these to exist even when logging to stdout/stderr.
RUN mkdir -p /usr/local/nginx/logs /usr/local/nginx/client_body_temp

EXPOSE 1935 8890

CMD ["nginx", "-c", "/usr/local/nginx/nginx.conf"]
