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

# xz is what the Nix installer unpacks with; the rest is what CGO and the
# odd Taskfile shell step expect to find.
RUN apk add --no-cache build-base libffi-dev openssl-dev zlib-dev curl bash git xz ca-certificates

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
RUN devbox install && devbox run -- go version && devbox run -- task --version

WORKDIR /app
