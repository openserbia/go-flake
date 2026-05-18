# go-flake

Pinned upstream Go releases (from [go.dev](https://go.dev/dl/)) packaged as a Nix flake. Useful when nixpkgs lags behind a Go security patch release and you want to stay on the upstream version without leaving devbox/Nix.

The flake fetches the official `tar.gz` binary distribution from `go.dev`, verifies it against the published SHA256, and (on Linux) patches the ELF interpreter so it runs inside Nix builds and on NixOS.

## Supported platforms

- `x86_64-linux`
- `aarch64-linux`
- `x86_64-darwin`
- `aarch64-darwin`

## Available versions

See [`versions.nix`](./versions.nix). Each version is exposed as `go_<major>_<minor>_<patch>` (dots replaced with underscores), plus a `default` / `go` alias for the newest entry.

## Usage with devbox

In `devbox.json`:

```json
{
  "packages": {
    "go": "github:openserbia/go-flake#go_1_26_3"
  }
}
```

Re-run `devbox shell` (or `devbox install`) and `go version` will report `go1.26.3`.

## Usage with plain Nix

```sh
nix run github:openserbia/go-flake -- version
nix shell github:openserbia/go-flake#go_1_26_3
```

## Adding a new Go release

1. Find the SHA256 sums published by go.dev:
   ```sh
   curl -fsS 'https://go.dev/dl/?mode=json&include=all' | \
     python3 -c "
   import json, sys
   target = 'go1.26.4'
   d = json.load(sys.stdin)
   for r in d:
     if r['version'] == target:
       for f in r['files']:
         if f['kind'] == 'archive' and f['os'] in ('linux','darwin') and f['arch'] in ('amd64','arm64'):
           print(f\"{f['filename']:40s} sha256={f['sha256']}\")
       break
   "
   ```
2. Add an entry to [`versions.nix`](./versions.nix) using the four sums.
3. Open a PR.

## Why this exists

When CVEs land in the Go stdlib (DNS resolver, `net/mail`, HTTP/2, etc.), upstream ships a patch release within days. nixpkgs maintainers usually follow within a week, but in the meantime production builds can be left exposed. This flake is a thin bridge: it mirrors `go.dev` exactly, so a one-line `versions.nix` change is enough to be back on a clean stdlib.

## License

The flake itself is MIT. The Go binary it distributes is upstream Google's, licensed under [BSD-3-Clause](https://go.dev/LICENSE).
