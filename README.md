# go-flake

> Daily-refreshed Nix flake mirroring upstream Go and Go tooling. Use it when
> nixpkgs lags an upstream security patch or tool release.

## At a glance

| Tool                                                  | Source                 | Pinning                            | Refresh |
|-------------------------------------------------------|------------------------|------------------------------------|---------|
| [`go`](https://go.dev)                                | `go.dev/dl/?mode=json` | published SHA256                   | daily   |
| [`golangci-lint`](https://golangci-lint.run)          | GitHub releases        | per-release `<name>-checksums.txt` | daily   |
| [`goreleaser`](https://goreleaser.com)                | GitHub releases        | per-release `checksums.txt`        | daily   |
| [`gofumpt`](https://github.com/mvdan/gofumpt)         | GitHub releases        | per-asset API `digest` field       | daily   |
| [`govulncheck`](https://golang.org/x/vuln)            | GitHub git tags        | source tarball + vendor SHA256     | daily   |
| [`gopls`](https://pkg.go.dev/golang.org/x/tools/gopls)| GitHub git tags        | source tarball + vendor SHA256     | daily   |

For the binary mirrors, every (os, arch) the upstream actually publishes is
mirrored — coverage is sparse per tool, since a tool appears on a system only
if its upstream ships a binary for that arch. `govulncheck` and `gopls` are
the exceptions: neither ships upstream binaries (Go's own tooling distributes
them via `go install`), so the flake builds them from source via
`buildGoModule` and exposes them on every system the flake spans. `gopls`
in particular is compiled against this flake's *own* latest Go rather than
nixpkgs' Go — gopls misbehaves when built with a Go minor older than the
project it's analyzing, so pinning the toolchain is what makes the
"no nixpkgs lag" guarantee hold for the language server.

## Quick start

### devbox

```json
{
  "packages": {
    "go": "github:openserbia/go-flake#go",
    "golangci-lint": "github:openserbia/go-flake#golangci-lint",
    "goreleaser": "github:openserbia/go-flake#goreleaser",
    "gofumpt": "github:openserbia/go-flake#gofumpt",
    "govulncheck": "github:openserbia/go-flake#govulncheck",
    "gopls": "github:openserbia/go-flake#gopls"
  }
}
```

Pin a specific version with `_<major>_<minor>_<patch>`, e.g. `#go_1_26_3`.

### plain Nix

```sh
nix run   github:openserbia/go-flake#go -- version
nix shell github:openserbia/go-flake#golangci-lint
nix build github:openserbia/go-flake#gofumpt_0_10_0
```

### Pointing JetBrains IDEs (GoLand / IntelliJ) at the SDK

The Go package installs under `$out/share/go/` (matching the nixpkgs
layout), with `$out/bin/{go,gofmt}` as symlinks into it. devbox merges the
package into its profile, so just point GoLand / IntelliJ's Go SDK at
`<project>/.devbox/nix/profile/default/share/go` — it has `bin/`, `src/`,
and `VERSION` at the top level, which is what the IDE validates against.

### Avoiding GitHub API rate limits

The `github:` URL scheme hits the GitHub API to resolve refs, which has a low
unauthenticated rate limit. For CI or shared environments that hit it, swap in
the `tarball+codeload` URL — same content, served via the codeload CDN:

```json
{
  "packages": {
    "go": "tarball+https://codeload.github.com/openserbia/go-flake/tar.gz/main#go_1_26_3"
  }
}
```

## Discover available versions

```sh
nix flake show github:openserbia/go-flake
```

Or browse the data files directly:

- [`versions.nix`](./versions.nix) — Go
- [`golangci-lint-versions.nix`](./golangci-lint-versions.nix)
- [`goreleaser-versions.nix`](./goreleaser-versions.nix)
- [`gofumpt-versions.nix`](./gofumpt-versions.nix)
- [`govulncheck-versions.nix`](./govulncheck-versions.nix)
- [`gopls-versions.nix`](./gopls-versions.nix)

Attribute naming: `<tool>_<major>_<minor>_<patch>` (dots → underscores). The
bare attribute `<tool>` is an alias for the newest version available on the
current system. `default` (and `go`) point at the latest Go.

## Platforms

Coverage matches each upstream's published artifacts; the flake exposes a tool
on a Nix system iff that system maps to a platform key in the data file.
Currently exposed systems:

| Linux               | Darwin           | FreeBSD          |
|---------------------|------------------|------------------|
| `x86_64-linux`      | `x86_64-darwin`  | `x86_64-freebsd` |
| `aarch64-linux`     | `aarch64-darwin` |                  |
| `i686-linux`        |                  |                  |
| `armv6l-linux`      |                  |                  |
| `armv7l-linux`      |                  |                  |
| `riscv64-linux`     |                  |                  |
| `powerpc64le-linux` |                  |                  |

Run `nix flake show` on your system to see which tools and versions resolve.

## How versions are refreshed

A daily self-hosted GitHub Actions workflow
([`.github/workflows/update-versions.yml`](./.github/workflows/update-versions.yml))
runs five idempotent updater scripts, then commits any diff straight to `main`:

| Script                                               | Updates                      | Trust source                                    |
|------------------------------------------------------|------------------------------|-------------------------------------------------|
| `scripts/update-versions.py`                         | `versions.nix`               | go.dev JSON's `sha256` field                    |
| `scripts/update-github-tool.py --tool golangci-lint` | `golangci-lint-versions.nix` | per-release `golangci-lint-<ver>-checksums.txt` |
| `scripts/update-github-tool.py --tool goreleaser`    | `goreleaser-versions.nix`    | per-release `checksums.txt`                     |
| `scripts/update-github-tool.py --tool gofumpt`       | `gofumpt-versions.nix`       | GitHub asset API `digest: sha256:…` field       |
| `scripts/update-source-tool.py --tool govulncheck`   | `govulncheck-versions.nix`   | `nix-prefetch-url --unpack` + `buildGoModule` vendor discovery |
| `scripts/update-source-tool.py --tool gopls`         | `gopls-versions.nix`         | `nix-prefetch-url --unpack` + `buildGoModule` vendor discovery |

Each script supports `--check` (exit non-zero if its data file is stale, no
write). All three updaters accept `--min-version` to lower the floor;
gofumpt's floor is fixed at `0.9.0` because GitHub only populates asset
digests for releases uploaded after mid-2024.

Trust chain for the binary mirrors: SHA256 comes from the channel above, then
Nix's own `fetchurl` verifies the downloaded archive against that hash at
build time — no intermediate proxy, URLs point at `go.dev` and
`github.com/<repo>/releases/download/...` directly. For `govulncheck` the
chain is two-step: the source tarball SHA pins the GitHub archive, and the
vendor hash pins the resolved Go module set against go.sum at build time.

## Why this exists

When CVEs land in the Go stdlib (DNS resolver, `net/mail`, HTTP/2, etc.),
upstream ships a patch within days. nixpkgs maintainers usually follow within a
week, but production builds can be left exposed in the interim. The same lag
hits major tool releases (golangci-lint, gofumpt) and frequently slips on Go
sub-minor cadence. This flake mirrors upstream exactly, so a one-line data-file
change is enough to be back on a clean stdlib or a current linter.

## License

This flake is MIT. Each mirrored binary is licensed by its upstream project
(Go: [BSD-3-Clause](https://go.dev/LICENSE); golangci-lint: GPL-3.0-or-later;
goreleaser: MIT; gofumpt: BSD-3-Clause; govulncheck: BSD-3-Clause).
