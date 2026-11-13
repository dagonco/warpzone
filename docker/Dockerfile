# The hub's container: `warpzone serve` (the library UI) and `warpzone catalog`, with the tools of
# `warpzone inbox` and `warpzone scrape`: RAHasher, dolphin-tool, nsz, orbis-pkg-util, chdman, bsdtar
# and Skyscraper.
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
#   Dolphin 2609b               commit 3a5b3743b18da3e8f04cbc44f20dc50fbd3a9ef3
#       (https://github.com/dolphin-emu/dolphin, annotated tag 2609b; its submodules are pinned by
#       that commit)
#   nsz 5.0.0                   docker/nsz-requirements.txt (every package with its SHA-256;
#       regenerate it with the command in docker/nsz-requirements.in)
#   orbis-pkg-util 0.1.0        crate SHA-256 940db8e0511cd2aa7e0199786fca75c34947e1fa0c92571c5b7881b175a597a8
#       (https://crates.io/crates/orbis-pkg-util; its crates are pinned with their checksums by the
#       Cargo.lock inside the crate, and the build refuses to change it)
#   Skyscraper 3.21.1           commit ccd49ae99a13ce037476f1d3cc3b646afca2059e
#       (https://github.com/Gemba/skyscraper, tag 3.21.1, a lightweight tag; it builds against the Qt 6
#       that Debian trixie packages, whose version the Debian release fixes)
#   rust:1.99.1-slim-trixie     sha256:f6c4a35d21dbb3c89d7a8d056c193e94fc09037f2b58dd858b09c39a47dd02d7
#       (the digest of the multi-platform index; only the orbis-pkg-util build uses it)
# Every stage but the Rust one uses the same Debian release, so what is built links against the glibc
# it runs on; the Rust one builds for the same Debian release (trixie) too.


# Stage 1: RAHasher with CHD support. Only the binary leaves this stage.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS rahasher

ARG RALIBRETRO_TAG=1.8.4
ARG RALIBRETRO_COMMIT=38c3741598b3551347fbce752c604d536d470d63

# bash and git are used by the Makefile's own src/RA_BuildVer.h rule (MakeBuildVer.sh).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git bash ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# libchdr's bundled zlib is built without its configure step, so its zconf.h does not include
# <unistd.h> and gzlib.c calls lseek undeclared, which GCC 14 (Debian trixie) refuses. Say that
# the header exists, and let GCC 14 treat the old C idioms of these vendored sources as warnings.
ARG RAHASHER_CFLAGS="-DHAVE_UNISTD_H -DZ_HAVE_UNISTD_H -fpermissive"

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
 && make -f Makefile.RAHasher HAVE_CHD=1 CC="gcc ${RAHASHER_CFLAGS}" -j"$(nproc)" \
 && install -D -m 0755 bin64/RAHasher /out/RAHasher \
 && /out/RAHasher 2>&1 | head -n 3


# Stage 2: dolphin-tool, the command line utility of Dolphin (Wii and GameCube disc images), and
# nothing else of Dolphin. Only the binary leaves this stage.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS dolphin

ARG DOLPHIN_TAG=2609b
ARG DOLPHIN_COMMIT=3a5b3743b18da3e8f04cbc44f20dc50fbd3a9ef3

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ninja-build git pkg-config ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# The tag is only a name: the build stops unless it is the pinned commit. Only the submodules the
# build compiles on Linux without a window, sound, Vulkan, Qt or the other optional parts are
# fetched, each at the commit the pinned Dolphin commit records.
RUN git clone --branch "${DOLPHIN_TAG}" --depth 1 https://github.com/dolphin-emu/dolphin.git dolphin \
 && cd dolphin \
 && test "$(git rev-parse HEAD)" = "${DOLPHIN_COMMIT}" \
 && git submodule update --init --depth 1 \
        Externals/bzip2/bzip2 Externals/cpp-ipc/cpp-ipc Externals/cpp-optparse/cpp-optparse \
        Externals/curl/curl Externals/enet/enet Externals/fmt/fmt Externals/glslang/glslang \
        Externals/hidapi/hidapi-src Externals/imgui/imgui Externals/implot/implot \
        Externals/libspng/libspng Externals/libusb/libusb Externals/lz4/lz4 \
        Externals/minizip-ng/minizip-ng Externals/pugixml/pugixml Externals/SFML/SFML \
        Externals/tinygltf/tinygltf Externals/watcher/watcher Externals/xxhash/xxHash \
        Externals/zlib-ng/zlib-ng Externals/zstd/zstd

# Every library is the one bundled in Dolphin's tree and linked statically (USE_SYSTEM_LIBS=OFF), so
# the binary needs only the C and C++ runtime libraries, which the last stage has. The video and
# sound backends, the window system, Qt and the network parts Dolphin has options for are off:
# dolphin-tool reads and converts disc images and never starts a game. Only the dolphin-tool
# target is built, with the libraries it links (the emulation core included, which is most of the
# build time).
RUN cd dolphin \
 && cmake -S . -B /build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DUSE_SYSTEM_LIBS=OFF \
        -DENABLE_CLI_TOOL=ON -DENABLE_QT=OFF -DENABLE_NOGUI=OFF -DENABLE_HEADLESS=ON \
        -DENABLE_TESTS=OFF -DENABLE_X11=OFF -DENABLE_EGL=OFF -DENABLE_VULKAN=OFF \
        -DENABLE_SDL=OFF -DENABLE_CUBEB=OFF -DENABLE_ALSA=OFF -DENABLE_PULSEAUDIO=OFF \
        -DENABLE_EVDEV=OFF -DENABLE_HWDB=OFF -DENABLE_LLVM=OFF -DENABLE_ANALYTICS=OFF \
        -DENABLE_AUTOUPDATE=OFF -DENABLE_LTO=OFF -DENCODE_FRAMEDUMPS=OFF \
        -DUSE_DISCORD_PRESENCE=OFF -DUSE_MGBA=OFF -DUSE_RETRO_ACHIEVEMENTS=OFF -DUSE_UPNP=OFF \
 && cmake --build /build --target dolphin-tool -j"$(nproc)" \
 && install -D -m 0755 "$(find /build -type f -name dolphin-tool | head -n 1)" /out/dolphin-tool \
 && strip /out/dolphin-tool \
 && ldd /out/dolphin-tool \
 && ! ldd /out/dolphin-tool | grep "not found" \
 && { /out/dolphin-tool 2>&1 || true; } | head -n 3


# Stage 3: nsz in a virtual environment of its own, so that its packages are not warpzone's. Every
# package is installed from a wheel, with the hash the requirements file records.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS nsz

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY docker/nsz-requirements.txt /tmp/nsz-requirements.txt
RUN python -m venv --without-pip /opt/nsz \
 && pip --python /opt/nsz/bin/python install --no-cache-dir --require-hashes --no-deps \
        --only-binary=:all: -r /tmp/nsz-requirements.txt \
 && /opt/nsz/bin/nsz --help | head -n 1


# Stage 4: orbis-pkg-util, which unpacks PS4 packages, from the crate published on crates.io. The
# download must have the pinned SHA-256 and the build must use the crate's own Cargo.lock.
FROM rust:1.99.1-slim-trixie@sha256:f6c4a35d21dbb3c89d7a8d056c193e94fc09037f2b58dd858b09c39a47dd02d7 AS orbis

ARG ORBIS_PKG_UTIL_VERSION=0.1.0
ARG ORBIS_PKG_UTIL_SHA256=940db8e0511cd2aa7e0199786fca75c34947e1fa0c92571c5b7881b175a597a8

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL -o orbis.crate \
        "https://static.crates.io/crates/orbis-pkg-util/orbis-pkg-util-${ORBIS_PKG_UTIL_VERSION}.crate" \
 && echo "${ORBIS_PKG_UTIL_SHA256}  orbis.crate" | sha256sum -c - \
 && tar -xzf orbis.crate \
 && cd "orbis-pkg-util-${ORBIS_PKG_UTIL_VERSION}" \
 && cargo build --release --locked \
 && install -D -m 0755 target/release/orbis-pkg-util /out/orbis-pkg-util \
 && strip /out/orbis-pkg-util \
 && /out/orbis-pkg-util --help | head -n 3


# Stage 5: Skyscraper (the Gemba fork), which `warpzone scrape` and `inbox` run to scrape game
# metadata and media. Qt 6 comes from Debian; the build links Qt's core, network, sql, xml and gui
# modules (the last one is QImage, for the artwork: no window is ever opened). BUNDLE_QRC=1 puts
# Skyscraper's own configuration files (peas.json, platforms_idmap.csv, the default artwork, the
# box and scanline images) inside the binary, so it is one file that needs nothing under
# /usr/local/etc and writes nothing but under its home folder. Only the binary leaves this stage.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS skyscraper

ARG SKYSCRAPER_TAG=3.21.1
ARG SKYSCRAPER_COMMIT=ccd49ae99a13ce037476f1d3cc3b646afca2059e

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates qt6-base-dev qmake6 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# The tag is only a name: the build stops unless it is the pinned commit.
RUN git clone --branch "${SKYSCRAPER_TAG}" --depth 1 https://github.com/Gemba/skyscraper.git skyscraper \
 && cd skyscraper \
 && test "$(git rev-parse HEAD)" = "${SKYSCRAPER_COMMIT}" \
 && BUNDLE_QRC=1 qmake6 \
 && make -j"$(nproc)" \
 && install -D -m 0755 Skyscraper /out/Skyscraper \
 && strip /out/Skyscraper \
 && ldd /out/Skyscraper \
 && ! ldd /out/Skyscraper | grep "not found" \
 && mkdir /tmp/sky-home \
 && HOME=/tmp/sky-home /out/Skyscraper --version


# Stage 6: warpzone from this checkout, as a wheel. It has no dependencies of its own.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS wheel

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY warpzone ./warpzone
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


# Stage 7: what runs. No compiler and no git; one process. The only packages it installs are the
# tools `warpzone inbox` needs that Debian packages, with the apt lists removed in the same layer;
# the others come from the stages above.
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d

LABEL org.opencontainers.image.title="warpzone hub" \
      org.opencontainers.image.description="warpzone serve and warpzone catalog, with the tools of warpzone inbox (RAHasher, chdman, bsdtar, dolphin-tool, nsz, orbis-pkg-util) and warpzone scrape (Skyscraper)" \
      org.opencontainers.image.source="https://github.com/dagonco/warpzone" \
      org.opencontainers.image.licenses="MIT"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir --no-index --no-deps /wheels/warpzone-*.whl \
 && rm -rf /wheels

# The tools `warpzone inbox` runs in the container, as Debian packages (they land in /usr/bin, on
# PATH): chdman, from mame-tools, converts discs to CHD; bsdtar, from libarchive-tools, unpacks
# .7z and .rar. The others are built or installed in the stages above (dolphin-tool, nsz,
# orbis-pkg-util, RAHasher, Skyscraper). Vita3K is not in the image; see
# website/src/content/docs/setup/inbox.md ("Where inbox runs").
# Skyscraper links Qt 6: its Core, Gui (images only), Network, Xml and Sql modules are the
# libqt6* packages, and libqt6sql6-sqlite is the SQL driver it loads at run time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends mame-tools libarchive-tools \
        libqt6gui6 libqt6network6 libqt6xml6 libqt6sql6 libqt6sql6-sqlite \
 && rm -rf /var/lib/apt/lists/* \
 && command -v chdman && command -v bsdtar

# RAHasher (GPL-3.0, RALibretro) and dolphin-tool (GPL-2.0-or-later, Dolphin) are built here from
# source and are not part of the repository. Each is a single file on PATH, in /usr/local/bin.
COPY --from=rahasher /out/RAHasher /usr/local/bin/RAHasher
COPY --from=dolphin /out/dolphin-tool /usr/local/bin/dolphin-tool

# orbis-pkg-util (MIT or Apache-2.0) is one file too.
COPY --from=orbis /out/orbis-pkg-util /usr/local/bin/orbis-pkg-util

# Skyscraper (GPL-3.0) is one file, and it keeps its config.ini and its cache in `~/.skyscraper`,
# which must be a volume: the root is read-only and /tmp is memory. HOME is /tmp for everything
# else (see below), so `Skyscraper` on PATH is a two-line script that runs the program with its own
# HOME, /opt/skyscraper-home, whose `.skyscraper` is a link to the volume mounted at /skyscraper.
# With nothing mounted the link dangles and Skyscraper stops at once (`warpzone doctor` says so).
# WARPZONE_SKYSCRAPER_DIR tells warpzone where config.ini is (the presence check of the account).
COPY --from=skyscraper /out/Skyscraper /opt/skyscraper/Skyscraper
RUN printf '%s\n' '#!/bin/sh' 'HOME=/opt/skyscraper-home; export HOME' 'exec /opt/skyscraper/Skyscraper "$@"' \
        > /usr/local/bin/Skyscraper \
 && chmod 0755 /usr/local/bin/Skyscraper \
 && mkdir /opt/skyscraper-home \
 && ln -s /skyscraper /opt/skyscraper-home/.skyscraper \
 && ! ldd /opt/skyscraper/Skyscraper | grep "not found"

# nsz (MIT) lives in its own virtual environment, /opt/nsz, which has its own copy of its
# packages; only its command is linked on PATH, so `python` and `pip` stay warpzone's.
COPY --from=nsz /opt/nsz /opt/nsz
RUN ln -s /opt/nsz/bin/nsz /usr/local/bin/nsz

# The config folder is mounted at /config; `[inbox] rahasher = "/usr/local/bin/RAHasher"` there.
# HOME is a place any uid can write to (the compose file mounts a tmpfs on it).
ENV WARPZONE_CONFIG=/config/config.toml \
    WARPZONE_SKYSCRAPER_DIR=/skyscraper \
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
