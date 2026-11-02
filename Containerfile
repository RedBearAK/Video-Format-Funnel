FROM docker.io/library/alpine:3.24

LABEL org.opencontainers.image.source="https://github.com/RedBearAK/Video-Format-Funnel"
LABEL org.opencontainers.image.title="vidfunnel"

RUN apk add --no-cache ffmpeg par2cmdline python3 py3-pip mediainfo

RUN python3 -m venv /opt/ffsubsync-venv \
    && /opt/ffsubsync-venv/bin/pip install --no-cache-dir ffsubsync \
    && ln -s /opt/ffsubsync-venv/bin/ffsubsync /usr/local/bin/ffsubsync

RUN set -eu; fail=0; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -qw libx264 || { echo "BUILD CHECK FAILED: ffmpeg lacks the libx264 encoder" >&2; fail=1; }; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q ' aac ' || { echo "BUILD CHECK FAILED: ffmpeg lacks the aac encoder" >&2; fail=1; }; \
    ffmpeg -hide_banner -demuxers 2>/dev/null | grep -qw dvdvideo || { echo "BUILD CHECK FAILED: ffmpeg lacks the dvdvideo demuxer" >&2; fail=1; }; \
    ffmpeg -hide_banner -bsfs 2>/dev/null | grep -qw h264_metadata || { echo "BUILD CHECK FAILED: ffmpeg lacks the h264_metadata bitstream filter" >&2; fail=1; }; \
    command -v par2 >/dev/null 2>&1 || { echo "BUILD CHECK FAILED: par2 is missing" >&2; fail=1; }; \
    command -v ffsubsync >/dev/null 2>&1 || { echo "BUILD CHECK FAILED: ffsubsync is missing" >&2; fail=1; }; \
    ffsubsync --version >/dev/null 2>&1 || { echo "BUILD CHECK FAILED: ffsubsync does not run" >&2; fail=1; }; \
    [ "$fail" -eq 0 ] || { echo "The base image or its packages changed; see the messages above." >&2; exit 1; }; \
    echo "Build checks passed: libx264, aac, dvdvideo, h264_metadata, par2, ffsubsync"

COPY vidfunnel/ /opt/vidfunnel/vidfunnel/
COPY bin/ /opt/vidfunnel/bin/
COPY LICENSE README.md /opt/vidfunnel/

ENTRYPOINT ["/opt/vidfunnel/bin/vidfunnel"]
CMD ["--version"]

# End of File #
