{
  description = "Pinned upstream Go releases (from go.dev) as Nix packages";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      versions = import ./versions.nix;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      goArchFor = system: {
        "x86_64-linux"   = "linux-amd64";
        "aarch64-linux"  = "linux-arm64";
        "x86_64-darwin"  = "darwin-amd64";
        "aarch64-darwin" = "darwin-arm64";
      }.${system};

      attrFor = v: "go_${builtins.replaceStrings [ "." ] [ "_" ] v}";
    in
    flake-utils.lib.eachSystem systems (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        lib = pkgs.lib;
        goArch = goArchFor system;
        isLinux = pkgs.stdenv.isLinux;

        mkGo = version:
          let spec = versions.${version};
          in pkgs.stdenvNoCC.mkDerivation {
            pname = "go";
            inherit version;
            src = pkgs.fetchurl {
              url = "https://go.dev/dl/go${version}.${goArch}.tar.gz";
              sha256 = spec.${goArch};
            };

            nativeBuildInputs = lib.optionals isLinux [ pkgs.autoPatchelfHook ];
            buildInputs = lib.optionals isLinux [
              pkgs.stdenv.cc.cc.lib
              pkgs.glibc
            ];

            # Test-data ELFs under src/debug/elf/testdata reference real-world libs
            # (e.g. libtiff.so.6) on purpose, to exercise the ELF parser. They
            # aren't meant to be loaded at runtime, so skip them in auto-patchelf.
            autoPatchelfIgnoreMissingDeps = [ "libtiff.so.6" ];

            dontConfigure = true;
            dontBuild = true;

            installPhase = ''
              runHook preInstall
              mkdir -p $out
              cp -r ./* $out/
              runHook postInstall
            '';

            meta = with lib; {
              description = "Go ${version} (upstream go.dev binary)";
              homepage = "https://go.dev";
              license = licenses.bsd3;
              platforms = systems;
              mainProgram = "go";
            };
          };

        availableVersions =
          builtins.filter
            (v: builtins.hasAttr goArch versions.${v})
            (builtins.attrNames versions);

        versionedPackages =
          builtins.listToAttrs
            (map (v: { name = attrFor v; value = mkGo v; }) availableVersions);

        # builtins.compareVersions splits on '.' and compares numerically,
        # so "1.24.13" > "1.24.9" as expected (string compare would invert it).
        latest = lib.last (builtins.sort
          (a: b: builtins.compareVersions a b < 0)
          availableVersions);
      in
      {
        packages = versionedPackages // {
          default = mkGo latest;
          go = mkGo latest;
        };
      });
}
