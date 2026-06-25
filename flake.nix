{
  description = "Pinned upstream Go releases and Go tooling (go.dev / GitHub) as Nix packages";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      goVersions           = import ./versions.nix;
      golangciLintVersions = import ./golangci-lint-versions.nix;
      goreleaserVersions   = import ./goreleaser-versions.nix;
      gofumptVersions      = import ./gofumpt-versions.nix;
      govulncheckVersions  = import ./govulncheck-versions.nix;
      goplsVersions        = import ./gopls-versions.nix;
      delveVersions        = import ./delve-versions.nix;
      staticcheckVersions  = import ./staticcheck-versions.nix;
      doclintVersions      = import ./doclint-versions.nix;

      # Per-tool: nix `system` -> the upstream platform key in that tool's
      # data file (e.g. "linux-amd64", "linux-armv7"). Sparse — if a system
      # isn't in a tool's table, the tool isn't exposed on that system.
      # govulncheck, gopls, delve, and staticcheck are built from source
      # via buildGoModule (upstream ships no binaries — or, for delve,
      # they're macOS-signed pkgs we don't want to mirror), so they aren't
      # keyed here — they're exposed on every system the flake evaluates over.
      #
      # Systems are restricted to ones where `nixpkgs.legacyPackages.<system>`
      # actually evaluates (excludes e.g. s390x-linux, loongarch64-linux,
      # aarch64-freebsd — upstreams publish for them but nixpkgs doesn't ship
      # a stdenv).
      #
      # Go's linux ARM is "linux-armv6l" only; armv7l-linux uses that build
      # too (armv7 is backwards-compatible with armv6). golangci-lint and
      # goreleaser publish armv6/armv7 separately. gofumpt's "linux-arm" is
      # semantically ambiguous (no v6/v7 distinction) so we don't map it.
      systemKey = {
        go = {
          "x86_64-linux"      = "linux-amd64";
          "aarch64-linux"     = "linux-arm64";
          "i686-linux"        = "linux-386";
          "armv6l-linux"      = "linux-armv6l";
          "armv7l-linux"      = "linux-armv6l";
          "riscv64-linux"     = "linux-riscv64";
          "powerpc64le-linux" = "linux-ppc64le";
          "x86_64-darwin"     = "darwin-amd64";
          "aarch64-darwin"    = "darwin-arm64";
          "x86_64-freebsd"    = "freebsd-amd64";
        };

        golangci-lint = {
          "x86_64-linux"      = "linux-amd64";
          "aarch64-linux"     = "linux-arm64";
          "i686-linux"        = "linux-386";
          "armv6l-linux"      = "linux-armv6";
          "armv7l-linux"      = "linux-armv7";
          "riscv64-linux"     = "linux-riscv64";
          "powerpc64le-linux" = "linux-ppc64le";
          "x86_64-darwin"     = "darwin-amd64";
          "aarch64-darwin"    = "darwin-arm64";
          "x86_64-freebsd"    = "freebsd-amd64";
        };

        goreleaser = {
          "x86_64-linux"   = "linux-amd64";
          "aarch64-linux"  = "linux-arm64";
          "i686-linux"     = "linux-386";
          "armv7l-linux"   = "linux-armv7";
          "riscv64-linux"  = "linux-riscv64";
          "x86_64-darwin"  = "darwin-amd64";
          "aarch64-darwin" = "darwin-arm64";
        };

        gofumpt = {
          "x86_64-linux"   = "linux-amd64";
          "aarch64-linux"  = "linux-arm64";
          "i686-linux"     = "linux-386";
          "x86_64-darwin"  = "darwin-amd64";
          "aarch64-darwin" = "darwin-arm64";
        };

        # doclint (openserbia) ships only linux/darwin × amd64/arm64.
        doclint = {
          "x86_64-linux"   = "linux-amd64";
          "aarch64-linux"  = "linux-arm64";
          "x86_64-darwin"  = "darwin-amd64";
          "aarch64-darwin" = "darwin-arm64";
        };
      };

      # Union of nix systems any tool exposes. The flake evaluates over this set.
      systems = builtins.attrNames (
        builtins.foldl' (acc: m: acc // m) {} (builtins.attrValues systemKey)
      );

      # version "1.26.3" -> "<prefix>_1_26_3"
      attrFor = prefix: v: "${prefix}_${builtins.replaceStrings [ "." ] [ "_" ] v}";

      # Sole maintainer of this flake; attached to every package's meta.
      # Named flakeMaintainers (not maintainers) so it doesn't read as a
      # shadow of lib.maintainers under mkGo's `with lib;`.
      flakeMaintainers = [{
        name = "OCharnyshevich";
        email = "4406080+OCharnyshevich@users.noreply.github.com";
        github = "OCharnyshevich";
        githubId = 4406080;
      }];

      # URL builders. Each takes (version, upstreamKey) and returns a URL.
      goUrl = version: key:
        "https://go.dev/dl/go${version}.${key}.tar.gz";

      golangciLintUrl = version: key:
        "https://github.com/golangci/golangci-lint/releases/download/v${version}/golangci-lint-${version}-${key}.tar.gz";

      # goreleaser uses capitalized OS + x86_64/i386 in filenames; convert back.
      goreleaserUrl = version: key:
        let
          parts = builtins.split "-" key;
          os = builtins.elemAt parts 0;
          arch = builtins.elemAt parts 2;
          capOs = (nixpkgs.lib.toUpper (builtins.substring 0 1 os))
                  + builtins.substring 1 (builtins.stringLength os) os;
          asset = if arch == "amd64" then "x86_64"
                  else if arch == "386" then "i386"
                  else arch;
        in
        "https://github.com/goreleaser/goreleaser/releases/download/v${version}/goreleaser_${capOs}_${asset}.tar.gz";

      # gofumpt assets use underscore between os and arch, no extension.
      gofumptUrl = version: key:
        "https://github.com/mvdan/gofumpt/releases/download/v${version}/gofumpt_v${version}_${builtins.replaceStrings [ "-" ] [ "_" ] key}";

      # doclint's GoReleaser archive: doclint_<version>_<os>_<arch>.tar.gz
      # (underscore between os and arch; systemKey "linux-amd64" -> "linux_amd64").
      doclintUrl = version: key:
        "https://github.com/openserbia/doclint/releases/download/v${version}/doclint_${version}_${builtins.replaceStrings [ "-" ] [ "_" ] key}.tar.gz";
    in
    flake-utils.lib.eachSystem systems (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        lib = pkgs.lib;
        isLinux = pkgs.stdenv.hostPlatform.isLinux;

        keyOf = tool: systemKey.${tool}.${system} or null;

        # Source-built tools route through nixpkgs' buildGoModule. On
        # x86_64-freebsd that hits an infinite-recursion bug in nixpkgs'
        # bmake/rsync stdenv setup — even a no-op
        # `buildGoModule { vendorHash = null; src = …; }` blows up at
        # eval time. Confirmed against minimal repros and reproduces on
        # plain nixpkgs without this flake's wrappers, so it's upstream.
        # Skip source-built tools on freebsd entirely; the binary mirrors
        # (go, golangci-lint) still work because they use stdenvNoCC +
        # fetchurl rather than buildGoModule.
        sourceBuiltSupported = !pkgs.stdenv.hostPlatform.isFreeBSD;

        # Platforms a source-built tool actually supports: every system the
        # flake spans except freebsd, where buildGoModule can't even evaluate
        # (see sourceBuiltSupported). Keeps meta.platforms honest instead of
        # claiming the whole `systems` union.
        sourceBuiltPlatforms = builtins.filter (s: !(lib.hasInfix "freebsd" s)) systems;

        sortAsc = vs: builtins.sort (a: b: builtins.compareVersions a b < 0) vs;

        # Versions whose data has a sum for the current system's key.
        availableFor = versions: tool:
          let k = keyOf tool; in
          if k == null then []
          else builtins.filter (v: builtins.hasAttr k versions.${v}) (builtins.attrNames versions);

        # mkGo: install the full Go tree under $out/share/go (matching the
        # nixpkgs layout), then symlink bin/{go,gofmt,...} into $out/bin
        # so devbox / nix `buildEnv` can put them on PATH without
        # dragging src/, pkg/, VERSION into the profile root. The
        # $out/share/go arrangement also lets JetBrains-family IDEs point
        # GOROOT at .devbox/nix/profile/default/share/go directly — they
        # validate by looking for bin/, src/, VERSION at that path.
        mkGo = version:
          let
            key = systemKey.go.${system};
            spec = goVersions.${version};
          in pkgs.stdenvNoCC.mkDerivation {
            pname = "go";
            inherit version;
            src = pkgs.fetchurl {
              url = goUrl version key;
              sha256 = spec.${key};
            };
            nativeBuildInputs = lib.optionals isLinux [ pkgs.autoPatchelfHook ];
            buildInputs = lib.optionals isLinux [
              pkgs.stdenv.cc.cc.lib
              pkgs.glibc
            ];
            # Test-data ELFs under src/debug/elf/testdata reference real-world
            # libs (e.g. libtiff.so.6); they aren't loaded at runtime, so we
            # skip them in auto-patchelf.
            autoPatchelfIgnoreMissingDeps = [ "libtiff.so.6" ];
            dontConfigure = true;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              mkdir -p $out/share/go $out/bin
              cp -r ./* $out/share/go/
              for b in $out/share/go/bin/*; do
                ln -s "../share/go/bin/$(basename "$b")" "$out/bin/$(basename "$b")"
              done
              runHook postInstall
            '';
            # buildGoModule reads three top-level attrs off the `go`
            # derivation: GOOS, GOARCH (module.nix:220) and CGO_ENABLED
            # (module.nix:225). Inheriting nixpkgs' values gives us the
            # right per-system defaults without recomputing them. Needed
            # for any caller that does `buildGoModule.override { go = …; }`
            # against this derivation (notably mkGopls).
            passthru = { inherit (pkgs.go) GOOS GOARCH CGO_ENABLED; };
            meta = with lib; {
              description = "Go ${version} (upstream go.dev binary)";
              homepage = "https://go.dev";
              changelog = "https://go.dev/doc/devel/release#go${versions.majorMinor version}";
              license = licenses.bsd3;
              # Mirrored prebuilt binary, not built from source — nixpkgs'
              # convention for such packages is to declare it explicitly.
              sourceProvenance = [ sourceTypes.binaryNativeCode ];
              platforms = builtins.attrNames systemKey.go;
              maintainers = flakeMaintainers;
              mainProgram = "go";
            };
          };

        # tar.gz tool: extract, find named binary, install to $out/bin/.
        mkArchivedTool = { pname, version, url, sha256, description, homepage, license, changelog }:
          pkgs.stdenvNoCC.mkDerivation {
            inherit pname version;
            src = pkgs.fetchurl { inherit url sha256; };
            nativeBuildInputs = lib.optionals isLinux [ pkgs.autoPatchelfHook ];
            buildInputs = lib.optionals isLinux [ pkgs.stdenv.cc.cc.lib ];
            unpackPhase = ''
              runHook preUnpack
              mkdir -p extracted
              tar -xzf "$src" -C extracted
              runHook postUnpack
            '';
            sourceRoot = "extracted";
            dontConfigure = true;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              bin="$(find . -type f -name '${pname}' -print -quit)"
              if [ -z "$bin" ]; then
                echo "error: ${pname} binary not found in archive" >&2
                exit 1
              fi
              install -Dm755 "$bin" "$out/bin/${pname}"
              runHook postInstall
            '';
            meta = {
              inherit description homepage license changelog;
              # Mirrored prebuilt binary, not built from source — nixpkgs'
              # convention for such packages is to declare it explicitly.
              sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
              platforms = builtins.attrNames systemKey.${pname};
              maintainers = flakeMaintainers;
              mainProgram = pname;
            };
          };

        # Bare-binary tool (e.g. gofumpt): no archive, $src is the binary.
        mkBareTool = { pname, version, url, sha256, description, homepage, license, changelog }:
          pkgs.stdenvNoCC.mkDerivation {
            inherit pname version;
            src = pkgs.fetchurl { inherit url sha256; };
            nativeBuildInputs = lib.optionals isLinux [ pkgs.autoPatchelfHook ];
            buildInputs = lib.optionals isLinux [ pkgs.stdenv.cc.cc.lib ];
            dontUnpack = true;
            dontConfigure = true;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              install -Dm755 "$src" "$out/bin/${pname}"
              runHook postInstall
            '';
            meta = {
              inherit description homepage license changelog;
              # Mirrored prebuilt binary, not built from source — nixpkgs'
              # convention for such packages is to declare it explicitly.
              sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
              platforms = builtins.attrNames systemKey.${pname};
              maintainers = flakeMaintainers;
              mainProgram = pname;
            };
          };

        mkGolangciLint = version:
          let k = systemKey.golangci-lint.${system};
              spec = golangciLintVersions.${version};
          in mkArchivedTool {
            pname = "golangci-lint";
            inherit version;
            url = golangciLintUrl version k;
            sha256 = spec.${k};
            description = "Fast linters runner for Go (upstream binary)";
            homepage = "https://golangci-lint.run";
            changelog = "https://github.com/golangci/golangci-lint/blob/v${version}/CHANGELOG.md";
            license = lib.licenses.gpl3Plus;
          };

        mkGoreleaser = version:
          let k = systemKey.goreleaser.${system};
              spec = goreleaserVersions.${version};
          in mkArchivedTool {
            pname = "goreleaser";
            inherit version;
            url = goreleaserUrl version k;
            sha256 = spec.${k};
            description = "Release-automation tool for Go projects (upstream binary)";
            homepage = "https://goreleaser.com";
            changelog = "https://github.com/goreleaser/goreleaser/releases/tag/v${version}";
            license = lib.licenses.mit;
          };

        mkGofumpt = version:
          let k = systemKey.gofumpt.${system};
              spec = gofumptVersions.${version};
          in mkBareTool {
            pname = "gofumpt";
            inherit version;
            url = gofumptUrl version k;
            sha256 = spec.${k};
            description = "Stricter gofmt (upstream binary)";
            homepage = "https://github.com/mvdan/gofumpt";
            changelog = "https://github.com/mvdan/gofumpt/releases/tag/v${version}";
            license = lib.licenses.bsd3;
          };

        # doclint: openserbia's own GoReleaser-published binary; same archive
        # shape as golangci-lint, so it routes through mkArchivedTool.
        mkDoclint = version:
          let k = systemKey.doclint.${system};
              spec = doclintVersions.${version};
          in mkArchivedTool {
            pname = "doclint";
            inherit version;
            url = doclintUrl version k;
            sha256 = spec.${k};
            description = "Hugo markdown + data-file linter with custom rules (openserbia binary)";
            homepage = "https://github.com/openserbia/doclint";
            changelog = "https://github.com/openserbia/doclint/releases/tag/v${version}";
            license = lib.licenses.mit;
          };

        # Latest mirrored Go for this system. Used as the toolchain for
        # source-built tools where pinning to the freshest Go matters
        # (notably gopls — see mkGopls). Falls back to nixpkgs.go if
        # this system has no Go entry (shouldn't happen for the systems
        # the flake exposes, but keeps the eval honest).
        latestGo =
          let avail = availableFor goVersions "go"; in
          if avail == [] then pkgs.go
          else mkGo (lib.last (sortAsc avail));

        # buildGoModule wired to use this flake's latest Go instead of
        # nixpkgs's. Match nixpkgs' own choice for gopls (buildGoLatestModule):
        # gopls misbehaves when compiled with a Go minor older than the
        # project it's analyzing, so it must track the latest Go release.
        # Keeping that property holds the flake's "no nixpkgs lag" promise
        # for the language server too.
        buildGoLatest = pkgs.buildGoModule.override { go = latestGo; };

        # govulncheck has no upstream binaries — build from source.
        # Like every other source-built tool here (and like nixpkgs'
        # buildGoLatestModule), compile against the flake's latest mirrored
        # Go rather than nixpkgs' — that's the whole point of the flake. The
        # vendorHash is the FOD hash of the resolved module set and is
        # specific to that version's go.sum.
        mkGovulncheck = version:
          let spec = govulncheckVersions.${version};
          in buildGoLatest {
            pname = "govulncheck";
            inherit version;
            src = pkgs.fetchFromGitHub {
              owner = "golang";
              repo = "vuln";
              rev = "v${version}";
              hash = spec.src;
            };
            vendorHash = spec.vendor;
            subPackages = [ "cmd/govulncheck" ];
            # fetchFromGitHub strips VCS info, so debug.ReadBuildInfo() reports
            # an empty/"(devel)" main version and govulncheck's scannerVersion()
            # falls back to a meaningless "v0.0.0". Bake the real version into
            # the same spot nixpkgs' version.patch does (verified byte-identical
            # across every mirrored govulncheck). --replace-fail (not -quiet) so
            # a future upstream rewrite of this block surfaces as a build failure
            # rather than silently regressing the reported version.
            postPatch = ''
              substituteInPlace internal/scan/run.go \
                --replace-fail 'if bi.Main.Version != "" && bi.Main.Version != "(devel)" {' 'if true {' \
                --replace-fail 'cfg.ScannerVersion = bi.Main.Version' 'cfg.ScannerVersion = "${version}"'
            '';
            ldflags = [ "-s" "-w" ];
            # The repo's tests reach out to vuln.go.dev and assume an
            # internet-connected workspace; skip them in the sandbox.
            doCheck = false;
            meta = {
              description = "Reports known vulnerabilities affecting Go code (built from source)";
              homepage = "https://pkg.go.dev/golang.org/x/vuln/cmd/govulncheck";
              changelog = "https://github.com/golang/vuln/releases/tag/v${version}";
              license = lib.licenses.bsd3;
              platforms = sourceBuiltPlatforms;
              maintainers = flakeMaintainers;
              mainProgram = "govulncheck";
            };
          };

        # delve ships a vendor/ directory in every release tarball, so
        # vendorHash = null is correct (buildGoModule uses the embedded
        # vendor/ directly). hardeningDisable disables FORTIFY_SOURCE —
        # delve's own runtime trips fortify checks when it compiles
        # throwaway debug helpers; matching nixpkgs' behavior keeps
        # CGO-based debugging working on hardened systems.
        mkDelve = version:
          let spec = delveVersions.${version};
          in buildGoLatest {
            pname = "delve";
            inherit version;
            src = pkgs.fetchFromGitHub {
              owner = "go-delve";
              repo = "delve";
              rev = "v${version}";
              hash = spec.src;
            };
            vendorHash = spec.vendor;
            nativeBuildInputs = [ pkgs.installShellFiles ];
            subPackages = [ "cmd/dlv" ];
            ldflags = [ "-s" "-w" ];
            hardeningDisable = [ "fortify" ];
            # delve's tests reach out to local sockets and assume a
            # connected workspace; skip them in the sandbox.
            doCheck = false;
            # dlv-dap is the binary name the VSCode Go extension launches for
            # DAP debugging; nixpkgs ships the same symlink. Shell completions
            # come straight from delve's own `completion` subcommand.
            postInstall = ''
              ln $out/bin/dlv $out/bin/dlv-dap
              installShellCompletion --cmd dlv \
                --bash <($out/bin/dlv completion bash) \
                --fish <($out/bin/dlv completion fish) \
                --zsh <($out/bin/dlv completion zsh)
            '';
            meta = {
              description = "Debugger for Go (built from source against this flake's latest Go)";
              homepage = "https://github.com/go-delve/delve";
              changelog = "https://github.com/go-delve/delve/blob/v${version}/CHANGELOG.md";
              license = lib.licenses.mit;
              platforms = sourceBuiltPlatforms;
              maintainers = flakeMaintainers;
              mainProgram = "dlv";
            };
          };

        # staticcheck is dominikh/go-tools' flagship binary; the repo also
        # ships a handful of other tools (structcheck, etc.) we don't
        # expose. subPackages narrows the build to just cmd/staticcheck.
        mkStaticcheck = version:
          let spec = staticcheckVersions.${version};
          in buildGoLatest {
            pname = "staticcheck";
            inherit version;
            src = pkgs.fetchFromGitHub {
              owner = "dominikh";
              repo = "go-tools";
              rev = version;
              hash = spec.src;
            };
            vendorHash = spec.vendor;
            subPackages = [ "cmd/staticcheck" ];
            doCheck = false;
            meta = {
              description = "Go linter applying advanced static-analysis checks (built from source against this flake's latest Go)";
              homepage = "https://staticcheck.dev";
              changelog = "https://github.com/dominikh/go-tools/releases/tag/${version}";
              license = lib.licenses.mit;
              platforms = sourceBuiltPlatforms;
              maintainers = flakeMaintainers;
              mainProgram = "staticcheck";
            };
          };

        # gopls is a subdirectory module of golang/tools, so modRoot points
        # at gopls/ and subPackages = [ "." ] builds that one module. The
        # tag form `gopls/v<version>` matches GitHub's archive URL too.
        mkGopls = version:
          let spec = goplsVersions.${version};
          in buildGoLatest {
            pname = "gopls";
            inherit version;
            src = pkgs.fetchFromGitHub {
              owner = "golang";
              repo = "tools";
              rev = "gopls/v${version}";
              hash = spec.src;
            };
            modRoot = "gopls";
            vendorHash = spec.vendor;
            # "." is gopls itself; modernize is the companion analyzer binary
            # nixpkgs also ships from this module. The cmd path is present in
            # every mirrored gopls (verified 0.20.0–0.22.0).
            subPackages = [
              "."
              "internal/analysis/modernize/cmd/modernize"
            ];
            # fetchFromGitHub strips VCS info, so without this gopls
            # reports `(devel)` instead of its real version at runtime.
            ldflags = [ "-X main.version=v${version}" ];
            doCheck = false;
            meta = {
              description = "Official language server for Go (built from source against this flake's latest Go)";
              homepage = "https://pkg.go.dev/golang.org/x/tools/gopls";
              changelog = "https://github.com/golang/tools/releases/tag/gopls/v${version}";
              license = lib.licenses.bsd3;
              platforms = sourceBuiltPlatforms;
              maintainers = flakeMaintainers;
              mainProgram = "gopls";
            };
          };

        # Build one tool's set of packages: versioned attrs + bare alias.
        toolPackages = { tool, versions, mkDrv }:
          let
            avail = availableFor versions tool;
            sorted = sortAsc avail;
            versioned = builtins.listToAttrs
              (map (v: { name = attrFor tool v; value = mkDrv v; }) avail);
            alias = if avail == [] then {} else { "${tool}" = mkDrv (lib.last sorted); };
          in versioned // alias;

        goPkgs            = toolPackages { tool = "go"; versions = goVersions; mkDrv = mkGo; };
        golangciLintPkgs  = toolPackages { tool = "golangci-lint"; versions = golangciLintVersions; mkDrv = mkGolangciLint; };
        goreleaserPkgs    = toolPackages { tool = "goreleaser"; versions = goreleaserVersions; mkDrv = mkGoreleaser; };
        gofumptPkgs       = toolPackages { tool = "gofumpt"; versions = gofumptVersions; mkDrv = mkGofumpt; };
        doclintPkgs       = toolPackages { tool = "doclint"; versions = doclintVersions; mkDrv = mkDoclint; };

        # govulncheck doesn't fit toolPackages — its data file is
        # `version -> {src, vendor}` (no per-system platform key) and
        # every version is buildable on every system the flake spans
        # (excluding freebsd — see sourceBuiltSupported).
        govulncheckPkgs =
          if !sourceBuiltSupported then {} else
          let
            avail = sortAsc (builtins.attrNames govulncheckVersions);
            versioned = builtins.listToAttrs
              (map (v: { name = attrFor "govulncheck" v; value = mkGovulncheck v; }) avail);
            alias = if avail == [] then {} else { govulncheck = mkGovulncheck (lib.last avail); };
          in versioned // alias;

        # Same shape as govulncheckPkgs — source-built tool with no
        # per-system platform key, exposed on every supported system.
        goplsPkgs =
          if !sourceBuiltSupported then {} else
          let
            avail = sortAsc (builtins.attrNames goplsVersions);
            versioned = builtins.listToAttrs
              (map (v: { name = attrFor "gopls" v; value = mkGopls v; }) avail);
            alias = if avail == [] then {} else { gopls = mkGopls (lib.last avail); };
          in versioned // alias;

        delvePkgs =
          if !sourceBuiltSupported then {} else
          let
            avail = sortAsc (builtins.attrNames delveVersions);
            versioned = builtins.listToAttrs
              (map (v: { name = attrFor "delve" v; value = mkDelve v; }) avail);
            alias = if avail == [] then {} else { delve = mkDelve (lib.last avail); };
          in versioned // alias;

        # staticcheck mixes 2-component (2026.1) and 3-component (2024.1.1)
        # versions. compareVersions handles both shapes, so the same
        # alphasort-by-compareVersions used elsewhere picks the right
        # newest entry for the alias.
        staticcheckPkgs =
          if !sourceBuiltSupported then {} else
          let
            avail = sortAsc (builtins.attrNames staticcheckVersions);
            versioned = builtins.listToAttrs
              (map (v: { name = attrFor "staticcheck" v; value = mkStaticcheck v; }) avail);
            alias = if avail == [] then {} else { staticcheck = mkStaticcheck (lib.last avail); };
          in versioned // alias;

        defaultPkg =
          let avail = availableFor goVersions "go"; in
          if avail == [] then {} else { default = mkGo (lib.last (sortAsc avail)); };
      in
      {
        packages = goPkgs // golangciLintPkgs // goreleaserPkgs // gofumptPkgs // doclintPkgs // govulncheckPkgs // goplsPkgs // delvePkgs // staticcheckPkgs // defaultPkg;
      });
}
