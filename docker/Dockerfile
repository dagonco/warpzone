# The hub's container: `warpzone serve` (the library UI) and `warpzone catalog`, with RAHasher.
# See website/src/content/docs/development/design/library-ui.md ("The container") and
# website/src/content/docs/setup/hub-container.md.
#
# Build from the root of a checkout, so that it is the checkout that gets installed:
#   docker build -f docker/Dockerfile -t warpzone:local .
#
# Pins (looked up on Docker Hub and GitHub on 2026-11-06). Move them together, by hand:
#   python:3.14.10-slim-trixie  sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d
#       (the digest of the multi-platform index, so amd64 and arm64 both resolve)
#   RALibretro 1.8.4            commit 38c3741598b3551347fbce752c604d536d470d63
#       (https://github.com/RetroAchievements/RALibretro, tag 1.8.4; its submodules are pinned by
#       that commit)
# Every stage uses the same Debian release, so RAHasher links against the glibc it runs on.


# Stage 1: RAHasher with CHD support. Only the binary leaves this stage.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS rahasher

ARG RALIBRETRO_TAG=1.8.4
ARG RALIBRETRO_COMMIT=38c3741598b3551347fbce752c604d536d470d63

# bash and git are used by the Makefile's own src/RA_BuildVer.h rule (MakeBuildVer.sh).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git bash ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# The tag is only a name: the build stops unless it is the pinned commit. Of the submodules only
# the ones RAHasher compiles (and MakeBuildVer.sh, which lives in RAInterface) are fetched, each
# at the commit the pinned RALibretro commit records; SDL2 and jsonsax are not needed.
RUN git clone --branch "${RALIBRETRO_TAG}" --depth 1 \
        https://github.com/RetroAchievements/RALibretro.git RALibretro \
 && cd RALibretro \
 && test "$(git rev-parse HEAD)" = "${RALIBRETRO_COMMIT}" \
 && git submodule update --init --depth 1 --recursive \
        src/RAInterface src/libchdr src/miniz src/rcheevos \
 && make -f Makefile.RAHasher HAVE_CHD=1 -j"$(nproc)" \
 && install -D -m 0755 bin64/RAHasher /out/RAHasher \
 && /out/RAHasher 2>&1 | head -n 3


# Stage 2: warpzone from this checkout, as a wheel. It has no dependencies of its own.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS wheel

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY warpzone ./warpzone
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


# Stage 3: what runs. No compiler, no git, no package index access; one process.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d

LABEL org.opencontainers.image.title="warpzone hub" \
      org.opencontainers.image.description="warpzone serve and warpzone catalog, with RAHasher" \
      org.opencontainers.image.source="https://github.com/dagonco/warpzone" \
      org.opencontainers.image.licenses="MIT"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir --no-index --no-deps /wheels/warpzone-*.whl \
 && rm -rf /wheels

# RAHasher is GPL-3.0 (RALibretro); it is built here from source and not part of the repository.
COPY --from=rahasher /out/RAHasher /usr/local/bin/RAHasher

# The config folder is mounted at /config; `[inbox] rahasher = "/usr/local/bin/RAHasher"` there.
# HOME is a place any uid can write to (the compose file mounts a tmpfs on it).
ENV WARPZONE_CONFIG=/config/config.toml \
    HOME=/tmp \
    PYTHONUNBUFFERED=1

# Not root by default. The compose file sets `user:` to the uid and gid that own the games share.
USER 65534:65534

EXPOSE 8780

# Keep `[serve] listen` on port 8780 inside the container and publish another port outside.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8780/healthz', timeout=3).read()"]

ENTRYPOINT ["warpzone"]
CMD ["serve"]
