#!/usr/bin/env python3
"""Refresh a source-built Go tool's data file from upstream GitHub tags.

For tools the flake builds with buildGoModule (no upstream binaries), each
version needs two hashes:
  - src:    sha256 of the unpacked GitHub source tarball
            (discovered via `nix-prefetch-url --unpack`)
  - vendor: sha256 of the Go module vendor tree resolved from go.sum
            (discovered by running buildGoModule with `lib.fakeHash` and
            parsing the "got: sha256-..." line out of the error)

Both discoveries require a working Nix on PATH. Honors $GITHUB_TOKEN.

This generalizes the previous update-govulncheck.py. Tools differ in tag
naming (e.g. `v1.3.0` vs `gopls/v0.22.0`), modRoot (a subdirectory go.mod
like `gopls/`), and which subpackage to build — encoded per-tool below.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolConfig:
    repo: str            # GitHub "owner/repo"
    min_version: str     # default floor, e.g. "1.0.0"
    output: str          # data file name under repo root
    pname: str           # nix pname (also the binary name expected)
    # Tag form on the upstream repo. The {ver} placeholder is the dotted
    # version. The same string is used for git tag lookups AND for the
    # GitHub archive URL — GitHub accepts slashes in the path.
    tag_fmt: str
    # Regex that matches a raw upstream tag and captures the version in
    # group 1. Used to filter `/tags` output back to a clean list.
    tag_re: re.Pattern
    # Subdir containing go.mod, "" for repo root. e.g. "gopls" for
    # golang/tools because the gopls module lives under tools/gopls/.
    mod_root: str
    # subPackages entry passed to buildGoModule. "." means "build the
    # package at mod_root" — used when mod_root already points at a
    # single-binary module (gopls). Otherwise something like
    # "cmd/govulncheck" relative to mod_root.
    sub_package: str
    # True for tools whose source tarball already contains a vendor/
    # directory — buildGoModule then uses that directory directly and
    # doesn't need a vendorHash. The script skips vendor discovery and
    # emits `vendor = null;` in the data file (e.g. delve).
    vendorless: bool = False


TOOLS: dict[str, ToolConfig] = {
    "govulncheck": ToolConfig(
        repo="golang/vuln",
        min_version="1.0.0",
        output="govulncheck-versions.nix",
        pname="govulncheck",
        tag_fmt="v{ver}",
        tag_re=re.compile(r"^v(\d+\.\d+\.\d+)$"),
        mod_root="",
        sub_package="cmd/govulncheck",
    ),
    "gopls": ToolConfig(
        repo="golang/tools",
        # gopls lives in golang/tools, which carries dozens of unrelated
        # tags (release-branch.go1.X, dl/*, internal/*, …). The tag_re
        # below picks out only `gopls/vX.Y.Z`, so the floor just decides
        # how far back we mirror.
        min_version="0.20.0",
        output="gopls-versions.nix",
        pname="gopls",
        tag_fmt="gopls/v{ver}",
        tag_re=re.compile(r"^gopls/v(\d+\.\d+\.\d+)$"),
        mod_root="gopls",
        sub_package=".",
    ),
    "delve": ToolConfig(
        repo="go-delve/delve",
        min_version="1.22.0",
        output="delve-versions.nix",
        pname="delve",
        tag_fmt="v{ver}",
        tag_re=re.compile(r"^v(\d+\.\d+\.\d+)$"),
        mod_root="",
        sub_package="cmd/dlv",
        # delve commits a vendor/ directory in every release tarball,
        # so buildGoModule doesn't need a vendor hash.
        vendorless=True,
    ),
    "staticcheck": ToolConfig(
        repo="dominikh/go-tools",
        # staticcheck (dominikh/go-tools) uses date-versioned tags
        # without a v-prefix, and mixes 2-component (2026.1) with
        # 3-component (2025.2.1) releases. The version parser handles
        # both shapes; the floor is the first release that builds with
        # modern Go modules.
        min_version="2024.1",
        output="staticcheck-versions.nix",
        pname="staticcheck",
        tag_fmt="{ver}",
        tag_re=re.compile(r"^(\d{4}\.\d+(?:\.\d+)?)$"),
        mod_root="",
        sub_package="cmd/staticcheck",
    ),
}


# Versions are stored as fixed-arity 3-tuples for orderable comparison
# even when an upstream uses 2-component releases (staticcheck ships
# `2026.1` and `2025.2.1` interchangeably). Missing components default
# to 0; the original dotted form is rebuilt from `dotted_version` so
# data files keep the upstream's own spelling.
def parse_version(s: str) -> tuple[int, int, int] | None:
    if any(c in s for c in "-+"):
        return None
    parts = s.split(".")
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def dotted_version(v: tuple[int, int, int], components: int) -> str:
    return ".".join(str(n) for n in v[:components])


def parse_min_version(s: str) -> tuple[int, int, int]:
    v = parse_version(s.lstrip("v"))
    if v is None:
        raise argparse.ArgumentTypeError(f"bad version {s!r}")
    return v


def _gh_headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "go-flake-update",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF = (1, 2, 4)
_RETRY_HTTP_CODES = {429, 500, 502, 503, 504}


def _fetch_retry(req: urllib.request.Request, read):
    last_err: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        if attempt > 0:
            time.sleep(_RETRY_BACKOFF[attempt - 1])
            print(
                f"  retry {attempt}/{_RETRY_ATTEMPTS - 1}: {req.full_url} "
                f"({type(last_err).__name__}: {last_err})",
                file=sys.stderr,
            )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return read(r)
        except urllib.error.HTTPError as e:
            if e.code not in _RETRY_HTTP_CODES:
                raise
            last_err = e
        except (
            http.client.IncompleteRead,
            socket.timeout,
            ConnectionError,
            urllib.error.URLError,
        ) as e:
            last_err = e
    assert last_err is not None
    raise last_err


def fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers=_gh_headers())
    return _fetch_retry(req, json.load)


def list_tags(cfg: ToolConfig, max_pages: int = 4) -> list[str]:
    # Walk /tags (authoritative version source — some upstreams stop
    # publishing GitHub Releases mid-life, e.g. golang/vuln after v1.1.4).
    # For repos that carry many unrelated tag namespaces (golang/tools
    # has gopls/, dl/, release-branch.*, …) max_pages may need to be
    # higher to reach enough matching tags; tag_re filters the rest out.
    per_page = 30
    out: list[str] = []
    for page in range(1, max_pages + 1):
        chunk = fetch_json(
            f"https://api.github.com/repos/{cfg.repo}/tags?per_page={per_page}&page={page}"
        )
        if not isinstance(chunk, list) or not chunk:
            break
        for t in chunk:
            name = t.get("name")
            if isinstance(name, str):
                out.append(name)
        if len(chunk) < per_page:
            break
    return out


# Allow 2- or 3-component versions (staticcheck mixes `2026.1` and `2025.2.1`).
NIX_VERSION_RE = re.compile(r'^\s*"(\d+(?:\.\d+){1,2})"\s*=\s*\{\s*$')
# `vendor` may be either a quoted SRI hash or the bare keyword `null`
# (used for vendorless tools like delve).
NIX_FIELD_RE = re.compile(
    r'^\s*(src|vendor)\s*=\s*(?:"(sha256-[A-Za-z0-9+/=]+)"|(null))\s*;\s*$'
)


# Per-version record: src is always an SRI hash; vendor is either an SRI
# hash or None (meaning "vendor = null;"); _dotted is the upstream's own
# dotted spelling, so 2-component releases round-trip as 2-component.
def parse_existing(text: str) -> dict[tuple[int, int, int], dict[str, str | None]]:
    out: dict[tuple[int, int, int], dict[str, str | None]] = {}
    cur_ver: tuple[int, int, int] | None = None
    cur_dotted: str | None = None
    cur_map: dict[str, str | None] = {}
    for line in text.splitlines():
        m = NIX_VERSION_RE.match(line)
        if m:
            dotted = m.group(1)
            v = parse_version(dotted)
            if v is not None:
                cur_ver = v
                cur_dotted = dotted
                cur_map = {}
            continue
        if cur_ver is not None and line.strip() == "};":
            if "src" in cur_map and "vendor" in cur_map:
                cur_map["_dotted"] = cur_dotted
                out[cur_ver] = cur_map
            cur_ver = None
            cur_dotted = None
            cur_map = {}
            continue
        m = NIX_FIELD_RE.match(line)
        if m and cur_ver is not None:
            # group(2) is the sha; group(3) is "null" when vendor is null.
            cur_map[m.group(1)] = m.group(2)  # None when the null branch matched
    return out


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def nix_prefetch_src(cfg: ToolConfig, version: str) -> str:
    """Compute the SRI sha256 of the GitHub source tarball for `version`."""
    tag = cfg.tag_fmt.format(ver=version)
    url = f"https://github.com/{cfg.repo}/archive/refs/tags/{tag}.tar.gz"
    r = _run(["nix-prefetch-url", "--unpack", "--type", "sha256", url])
    if r.returncode != 0:
        raise RuntimeError(f"nix-prefetch-url failed for {tag}: {r.stderr.strip()}")
    base32 = r.stdout.strip().splitlines()[-1]
    sri = _run([
        "nix", "hash", "convert",
        "--hash-algo", "sha256", "--to", "sri", base32,
    ])
    if sri.returncode != 0:
        raise RuntimeError(f"hash convert failed for {tag}: {sri.stderr.strip()}")
    return sri.stdout.strip()


def _vendor_expr(cfg: ToolConfig) -> str:
    owner, repo = cfg.repo.split("/", 1)
    mod_root_line = f'modRoot = "{cfg.mod_root}";' if cfg.mod_root else ""
    # Two levels of substitution happen here:
    #   - Python f-string: `{...}` interpolates Python expressions, so
    #     literal braces are written `{{` `}}`.
    #   - Nix indented string: this whole body gets wrapped in ''...''
    #     and passed to builtins.toFile. Inside ''...'' the outer Nix
    #     evaluator antiquotes `${...}` against its own scope (which
    #     doesn't have `version` or anything else useful), so every
    #     `${X}` we want passed through to the inner file is escaped
    #     as `''${X}`. (Earlier versions of this script silently lost
    #     antiquotations to the outer evaluator, leaving vendor hashes
    #     unresolvable — the data files were hand-populated instead.)
    nix_rev = cfg.tag_fmt.replace("{ver}", "''${version}")
    return f"""
{{ version, srcHash }}:
let
  flake = builtins.getFlake "nixpkgs";
  pkgs = flake.legacyPackages.''${{builtins.currentSystem}};
in
pkgs.buildGoModule {{
  pname = "{cfg.pname}";
  inherit version;
  src = pkgs.fetchFromGitHub {{
    owner = "{owner}";
    repo = "{repo}";
    rev = "{nix_rev}";
    hash = srcHash;
  }};
  {mod_root_line}
  vendorHash = pkgs.lib.fakeHash;
  subPackages = [ "{cfg.sub_package}" ];
  doCheck = false;
}}
"""


def nix_discover_vendor(cfg: ToolConfig, version: str, src_hash: str) -> str:
    expr = (
        f"import (builtins.toFile \"v.nix\" ''{_vendor_expr(cfg)}'') "
        f"{{ version = \"{version}\"; srcHash = \"{src_hash}\"; }}"
    )
    r = _run([
        "nix", "build",
        "--extra-experimental-features", "nix-command flakes",
        "--no-link", "--impure",
        "--expr", expr,
    ])
    combined = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"got:\s*(sha256-[A-Za-z0-9+/=]+)", combined)
    if m:
        return m.group(1)
    raise RuntimeError(
        f"could not extract vendorHash for {cfg.pname} v{version}; nix output:\n{combined.strip()}"
    )


def collect(
    cfg: ToolConfig,
    existing: dict[tuple[int, int, int], dict[str, str | None]],
    min_version: tuple[int, int, int],
) -> dict[tuple[int, int, int], dict[str, str | None]]:
    out = {v: dict(m) for v, m in existing.items() if v >= min_version}
    for tag in list_tags(cfg):
        m = cfg.tag_re.match(tag)
        if m is None:
            continue
        dotted = m.group(1)
        v = parse_version(dotted)
        if v is None or v < min_version or v in out:
            continue
        print(f"  resolving {cfg.tag_fmt.format(ver=dotted)}", file=sys.stderr)
        try:
            src = nix_prefetch_src(cfg, dotted)
            vendor = None if cfg.vendorless else nix_discover_vendor(cfg, dotted, src)
        except RuntimeError as e:
            print(f"  skip {dotted}: {e}", file=sys.stderr)
            continue
        out[v] = {"src": src, "vendor": vendor, "_dotted": dotted}
    return out


def render(versions: dict[tuple[int, int, int], dict[str, str | None]]) -> str:
    if not versions:
        return "{\n}\n"
    lines = ["{"]
    for v in sorted(versions):
        spec = versions[v]
        dotted = spec.get("_dotted") or ".".join(str(n) for n in v)
        vendor = spec["vendor"]
        vendor_field = "null" if vendor is None else f'"{vendor}"'
        lines.append(f'  "{dotted}" = {{')
        lines.append(f'    src    = "{spec["src"]}";')
        lines.append(f'    vendor = {vendor_field};')
        lines.append("  };")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tool", choices=sorted(TOOLS), required=True)
    ap.add_argument(
        "--min-version", type=parse_min_version, default=None,
        help="Override the tool's default minimum version",
    )
    ap.add_argument(
        "-o", "--output", default=None,
        help="Override output path (default: <repo>/<tool default>)",
    )
    ap.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if output would differ from existing file (no write). "
             "Hits the network to discover new upstream versions.",
    )
    ap.add_argument(
        "--validate", action="store_true",
        help="Exit non-zero if the existing data file isn't round-trip stable "
             "(parse + render produces different bytes). No network. Use in CI.",
    )
    args = ap.parse_args()

    cfg = TOOLS[args.tool]
    min_ver = args.min_version or parse_version(cfg.min_version)
    assert min_ver is not None
    out_path = Path(args.output) if args.output else repo_root / cfg.output

    existing = parse_existing(out_path.read_text()) if out_path.exists() else {}

    if args.validate:
        if not out_path.exists():
            print(f"{out_path} does not exist", file=sys.stderr)
            return 1
        rendered = render(existing)
        cur = out_path.read_text()
        if cur != rendered:
            print(f"{out_path} is not round-trip stable", file=sys.stderr)
            return 1
        print(
            f"{out_path} round-trips cleanly ({len(existing)} versions)",
            file=sys.stderr,
        )
        return 0

    print(
        f"fetching tags for {cfg.repo} "
        f"({len(existing)} versions already on disk)",
        file=sys.stderr,
    )
    versions = collect(cfg, existing, min_ver)
    if not versions:
        print("error: no matching versions found", file=sys.stderr)
        return 1

    rendered = render(versions)
    min_str = ".".join(str(n) for n in min_ver)
    print(f"collected {len(versions)} versions (>= {min_str})", file=sys.stderr)

    if args.check:
        cur = out_path.read_text() if out_path.exists() else ""
        if cur != rendered:
            print(f"{out_path} is out of date", file=sys.stderr)
            return 1
        print(f"{out_path} is up to date", file=sys.stderr)
        return 0

    out_path.write_text(rendered)
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
