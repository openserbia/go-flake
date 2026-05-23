# go-flake — notes for Claude

This flake mirrors upstream Go and Go tooling. Read `README.md` for the
user-facing pitch; this file documents the patterns and gotchas that aren't
obvious from reading the code.

## Two tool patterns

Tools fall into one of two buckets. Pick the bucket first, then follow the
recipe — don't mix mechanisms.

### Binary mirror (upstream ships prebuilt binaries)

Used by: `go`, `golangci-lint`, `goreleaser`, `gofumpt`.

- Data file: `{version: {os-arch: sha256-hex}}` per platform key.
- `systemKey.<tool>` in `flake.nix` maps nix systems to the upstream's
  platform spelling. Sparse — a tool is only exposed on a system if there's
  an entry.
- Updater: `scripts/update-github-tool.py --tool <name>` (or
  `update-versions.py` for `go` itself).
- Builder: `mkArchivedTool` for tarballs, `mkBareTool` for naked binaries.
  Both run `autoPatchelfHook` on Linux to relocate the binary into the
  Nix store.

### Source-built (no usable upstream binaries)

Used by: `govulncheck`, `gopls`, `delve`, `staticcheck`.

- Data file: `{version: {src, vendor}}`. `vendor` is either an SRI hash
  or the bare keyword `null` (when the tarball ships its own `vendor/`).
- No `systemKey` entry — these are exposed on every system the flake spans
  because `buildGoModule` handles cross-platform itself.
- Updater: `scripts/update-source-tool.py --tool <name>`. Per-tool config
  lives in the `TOOLS` dict at the top of that script.
- Builder: per-tool `mk<Name>` in `flake.nix`, always using `buildGoLatest`
  (i.e. `buildGoModule.override { go = latestGo; }`) — never plain
  `pkgs.buildGoModule`. The whole point of this flake is that source-built
  Go tooling tracks the freshest mirrored Go, not whatever nixpkgs has.

## Recipes

### Add a source-built tool

1. Add a `ToolConfig` entry to `TOOLS` in `scripts/update-source-tool.py`.
   Fields that matter:
   - `tag_fmt` / `tag_re` — upstream's tag spelling. The `{ver}` placeholder
     in `tag_fmt` is also the GitHub archive URL component, so it must match
     `tag_re`'s captured group exactly. Tags with slashes (`gopls/v0.22.0`)
     work — GitHub archives accept them.
   - `mod_root` — `""` unless the go.mod lives in a subdirectory (e.g.
     `gopls/` inside `golang/tools`).
   - `sub_package` — relative to `mod_root`. Use `"."` to build the
     `mod_root` package itself.
   - `vendorless=True` if the release tarball ships a `vendor/` directory
     (check with `tar -tzf … | grep -E '/vendor/'`). Delve does, gopls
     doesn't.
2. Create an empty `<tool>-versions.nix` containing just `{}`. The updater
   will populate it on first run.
3. Run `./scripts/update-source-tool.py --tool <name>` and commit the
   resulting data file.
4. In `flake.nix`:
   - Add the `import` line beside the other `<tool>Versions` bindings.
   - Add `mk<Tool>` next to `mkGopls` / `mkDelve` etc. Always wrap with
     `buildGoLatest`. Set `meta.mainProgram` to the actual binary name —
     it can differ from `pname` (delve produces `dlv`).
   - Add `<tool>Pkgs` block mirroring `goplsPkgs`/`delvePkgs`.
   - Append `// <tool>Pkgs` to the final `packages = …` merge.
5. In `.github/workflows/update-versions.yml`:
   - Add a step calling `update-source-tool.py --tool <name>`.
   - Add `<tool>-versions.nix` to the `files="…"` shell variable. Forgetting
     this means the daily refresh works but never commits.
6. In `README.md`: add a row to the "At a glance" table, a row to the
   updaters table, a list entry under "Discover available versions", and an
   entry in the devbox quick-start JSON.

### Add a binary-mirrored tool

Mirror the same shape as `gofumpt` (bare binary) or `golangci-lint`
(archive). Add a `ToolConfig` entry to `TOOLS` in
`scripts/update-github-tool.py`, mode `digest` or `checksums`. Add a
`systemKey.<tool>` table in `flake.nix` listing every nix system the
upstream publishes for. The same workflow/README steps apply.

## Gotchas

- **`buildGoModule.override { go = ourGo; }` requires passthru attrs on
  `ourGo`**. `pkgs/build-support/go/module.nix` reads three attrs off the
  Go derivation: `GOOS`, `GOARCH` (line 220), `CGO_ENABLED` (line 225).
  `mkGo` sets `passthru = { inherit (pkgs.go) GOOS GOARCH CGO_ENABLED; }`
  to inherit the per-system values from nixpkgs. If buildGoModule grows
  another required attr in a future nixpkgs bump, expect a `missing
  attribute` error from a source-built tool and add it to the passthru.

- **`${...}` antiquotation in `_vendor_expr`** (script side). The body of
  `_vendor_expr` gets wrapped in a Nix `''...''` indented string by
  `nix_discover_vendor`. Inside `''...''`, the outer evaluator antiquotes
  `${X}` against its own scope. Any antiquotation that should land
  literally in the inner file must be escaped as `''${X}`. The original
  `update-govulncheck.py` got this wrong silently — vendor hashes never
  resolved, and the data file was hand-populated. Fixed in 84d0110.

- **`fetchFromGitHub` strips VCS info**. Tools that read their version
  from `runtime/debug.ReadBuildInfo()` will report `(devel)` unless we
  inject the version via `ldflags = [ "-X main.version=v${version}" ]`.
  gopls needs this; delve and staticcheck have their own version-baking
  mechanisms and don't.

- **Idempotency check exists**. `--check` (exit non-zero on diff, no
  write) is wired up on every updater. CI doesn't currently use it, but
  it's useful locally before committing.

- **Mixed-component versions**. `staticcheck` ships both 2-component
  (`2026.1`) and 3-component (`2025.1.1`) releases. The version parser
  pads to 3-component for sorting and stores the original dotted form in
  `_dotted` so the data file round-trips. Don't normalize the spelling.

- **delve fortify**. `hardeningDisable = [ "fortify" ]` is necessary on
  hardened systems for CGO-based debugging; we skip the additional
  `disable-fortify.diff` that nixpkgs carries because vendoring a
  version-sensitive binary patch into this repo isn't worth the
  maintenance cost for the marginal coverage gain. Revisit if reports
  come in.

## Style

- Conventional Commits (`feat:`, `fix:`, `chore:`, `perf:`), lowercase,
  short title. Body explains the *why* in 1–3 sentences. See `git log`.
- Comments explain non-obvious *why*, never *what*. The repo leans toward
  carrying long-form rationale at the top of helper functions / above
  awkward escape sequences (see `mkGo`'s passthru comment and
  `_vendor_expr`'s escape comment) — match that depth when the
  surrounding context warrants it.
- Don't commit `scripts/__pycache__/` — it's transient.
