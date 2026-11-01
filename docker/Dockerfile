# Shared Go builder base for openserbia services.
#
# Every service Dockerfile used to open with the same four steps: Alpine,
# `curl | bash` for devbox, `devbox install`, then `task build`. Each build
# re-downloaded the installer (the get.jetpack.io launcher has no retry and
# a transient TLS error there has failed deploys) and re-provisioned a Nix
# store from scratch whenever a devbox.json changed. This image does that
# once, here, and the services start from it:
#
#   FROM ghcr.io/openserbia/go-builder:latest AS builder
#   COPY devbox.json devbox.lock ./
#   RUN devbox install          # near no-op: the store is already warm
#   RUN devbox run -- task build
#
# It is a builder stage only. Runtime images stay distroless; nothing here
# is meant to be shipped.
FROM alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b

ARG DEVBOX_VERSION=0.17.2
ARG TARGETARCH

# xz unpacks the Nix installer. bash and ca-certificates are what that
# installer and devbox need. git stays the Alpine binary: Taskfiles call
# it outside the Nix profile, and the nixpkgs git closure pulls Python
# and Perl. No C toolchain here. Image builds set CGO_ENABLED=0 and ship
# distroless static; a service that actually compiles C puts gcc in its
# own devbox.json.
RUN apk add --no-cache bash ca-certificates curl git xz

# Pinned devbox from GitHub releases, not the launcher script: the launcher
# resolves "latest" against releases.jetify.com with no retry. Bump the ARG
# deliberately; the image tag carries it.
RUN curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
      "https://github.com/jetify-com/devbox/releases/download/${DEVBOX_VERSION}/devbox_${DEVBOX_VERSION}_linux_${TARGETARCH:-amd64}.tar.gz" \
    | tar -xz -C /usr/local/bin devbox \
    && devbox version

# Warm the Nix store with the toolchain every service shares. devbox
# installs single-user Nix on this first run. Go and the linters are the
# flake's unversioned attributes on purpose: they resolve to the newest
# release the flake mirrors, so when the services bump their pinned
# version the base already holds it. A consumer's own `devbox install`
# only fetches what its lock adds on top, and store paths depend on the
# tool version and the nixpkgs revision, not on which go-flake commit the
# consumer locked, so a matching version is a cache hit across lock drift
# and a mismatch merely costs the download.
WORKDIR /warm
COPY devbox.json ./
# devbox install realises a stdenv, the upstream tarballs, and a nixpkgs
# checkout, then leaves the build-only paths in the store. Pin the flake
# inputs that were actually fetched so the next eval does not re-download
# nixpkgs, then drop everything the profile does not reference.
RUN devbox install \
 && devbox run -- go version \
 && devbox run -- golangci-lint version \
 && devbox run -- task --version \
 && export PATH="/nix/var/nix/profiles/default/bin:$PATH" \
 && mkdir -p /nix/var/nix/gcroots/warm \
 && nix --extra-experimental-features 'nix-command flakes' flake archive --json --dry-run /warm/.devbox/gen/flake > /tmp/flake-archive.json \
 && i=0 \
 && for p in $(grep -o '/nix/store/[^"]*' /tmp/flake-archive.json); do \
      if [ -e "$p" ]; then ln -sfn "$p" "/nix/var/nix/gcroots/warm/input-$i"; i=$((i+1)); fi; \
    done \
 && nix-collect-garbage -d \
 && rm -rf /root/.cache /tmp/* /warm/.devbox/.nix-print-dev-env-cache

WORKDIR /app
