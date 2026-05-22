#!/usr/bin/env python3
"""Refresh govulncheck-versions.nix from upstream golang/vuln releases.

govulncheck publishes no prebuilt binaries — only source tags. So unlike the
other tools in this flake, each version needs two hashes:
  - src:    sha256 of the unpacked GitHub source tarball
            (discovered via `nix-prefetch-url --unpack`)
  - vendor: sha256 of the Go module vendor tree resolved from go.sum
            (discovered by running buildGoModule with `lib.fakeHash` and
            parsing the "got: sha256-..." line out of the error)

Both discoveries require a working Nix on PATH. Honors $GITHUB_TOKEN.
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
from pathlib import Path


REPO = "golang/vuln"
MIN_VERSION = (1, 0, 0)
OUTPUT = "govulncheck-versions.nix"

# Reused from update-github-tool.py — different file because the data shape
# (per-version {src, vendor} dict, no per-platform keys) and discovery
# mechanism (nix-prefetch + buildGoModule fake-hash) are different enough
# that sharing infrastructure would obscure both paths.


def parse_version(tag: str) -> tuple[int, int, int] | None:
    if tag.startswith("v"):
        tag = tag[1:]
    if any(c in tag for c in "-+"):
        return None
    parts = tag.split(".")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


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


def list_tags(max_pages: int = 2) -> list[str]:
    # golang/vuln stopped publishing GitHub Releases after v1.1.4 — newer
    # versions exist only as git tags. So we walk /tags (which is the
    # authoritative version source) rather than /releases.
    per_page = 30
    out: list[str] = []
    for page in range(1, max_pages + 1):
        chunk = fetch_json(
            f"https://api.github.com/repos/{REPO}/tags?per_page={per_page}&page={page}"
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


# Existing data-file parser. The two value rows ("src" / "vendor") are SRI
# hashes so the regex tolerates the base64 alphabet rather than locking to hex.
NIX_VERSION_RE = re.compile(r'^\s*"(\d+\.\d+\.\d+)"\s*=\s*\{\s*$')
NIX_FIELD_RE = re.compile(r'^\s*(src|vendor)\s*=\s*"(sha256-[A-Za-z0-9+/=]+)"\s*;\s*$')


def parse_existing(text: str) -> dict[tuple[int, int, int], dict[str, str]]:
    out: dict[tuple[int, int, int], dict[str, str]] = {}
    cur_ver: tuple[int, int, int] | None = None
    cur_map: dict[str, str] = {}
    for line in text.splitlines():
        m = NIX_VERSION_RE.match(line)
        if m:
            v = parse_version(m.group(1))
            if v is not None:
                cur_ver = v
                cur_map = {}
            continue
        if cur_ver is not None and line.strip() == "};":
            if {"src", "vendor"}.issubset(cur_map):
                out[cur_ver] = cur_map
            cur_ver = None
            cur_map = {}
            continue
        m = NIX_FIELD_RE.match(line)
        if m and cur_ver is not None:
            cur_map[m.group(1)] = m.group(2)
    return out


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def nix_prefetch_src(version: str) -> str:
    """Compute the SRI sha256 of v<version>'s GitHub source tarball."""
    url = f"https://github.com/{REPO}/archive/refs/tags/v{version}.tar.gz"
    r = _run(["nix-prefetch-url", "--unpack", "--type", "sha256", url])
    if r.returncode != 0:
        raise RuntimeError(f"nix-prefetch-url failed for v{version}: {r.stderr.strip()}")
    base32 = r.stdout.strip().splitlines()[-1]
    sri = _run([
        "nix", "hash", "convert",
        "--hash-algo", "sha256", "--to", "sri", base32,
    ])
    if sri.returncode != 0:
        raise RuntimeError(f"hash convert failed for v{version}: {sri.stderr.strip()}")
    return sri.stdout.strip()


_VENDOR_EXPR = """
{ version, srcHash }:
let
  flake = builtins.getFlake "nixpkgs";
  pkgs = flake.legacyPackages.${builtins.currentSystem};
in
pkgs.buildGoModule {
  pname = "govulncheck";
  inherit version;
  src = pkgs.fetchFromGitHub {
    owner = "golang";
    repo = "vuln";
    rev = "v${version}";
    hash = srcHash;
  };
  vendorHash = pkgs.lib.fakeHash;
  subPackages = [ "cmd/govulncheck" ];
  doCheck = false;
}
"""


def nix_discover_vendor(version: str, src_hash: str) -> str:
    """Run buildGoModule with a fake hash and extract the real vendor hash."""
    expr = (
        f"import (builtins.toFile \"v.nix\" ''{_VENDOR_EXPR}'') "
        f"{{ version = \"{version}\"; srcHash = \"{src_hash}\"; }}"
    )
    r = _run([
        "nix", "build",
        "--extra-experimental-features", "nix-command flakes",
        "--no-link", "--impure",
        "--expr", expr,
    ])
    # The fake-hash trick always fails; we want the "got: sha256-..." line.
    combined = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"got:\s*(sha256-[A-Za-z0-9+/=]+)", combined)
    if m:
        return m.group(1)
    raise RuntimeError(
        f"could not extract vendorHash for v{version}; nix output:\n{combined.strip()}"
    )


def collect(
    existing: dict[tuple[int, int, int], dict[str, str]],
    min_version: tuple[int, int, int],
) -> dict[tuple[int, int, int], dict[str, str]]:
    out = {v: dict(m) for v, m in existing.items() if v >= min_version}
    for tag in list_tags():
        v = parse_version(tag)
        if v is None or v < min_version or v in out:
            continue
        ver_str = ".".join(str(n) for n in v)
        print(f"  resolving v{ver_str}", file=sys.stderr)
        try:
            src = nix_prefetch_src(ver_str)
            vendor = nix_discover_vendor(ver_str, src)
        except RuntimeError as e:
            print(f"  skip v{ver_str}: {e}", file=sys.stderr)
            continue
        out[v] = {"src": src, "vendor": vendor}
    return out


def render(versions: dict[tuple[int, int, int], dict[str, str]]) -> str:
    if not versions:
        return "{\n}\n"
    lines = ["{"]
    for v in sorted(versions):
        ver = ".".join(str(n) for n in v)
        lines.append(f'  "{ver}" = {{')
        lines.append(f'    src    = "{versions[v]["src"]}";')
        lines.append(f'    vendor = "{versions[v]["vendor"]}";')
        lines.append("  };")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-o", "--output", default=None,
        help=f"Override output path (default: <repo>/{OUTPUT})",
    )
    ap.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if output would differ from existing file (no write)",
    )
    args = ap.parse_args()

    out_path = Path(args.output) if args.output else repo_root / OUTPUT
    existing = parse_existing(out_path.read_text()) if out_path.exists() else {}
    print(
        f"fetching tags for {REPO} "
        f"({len(existing)} versions already on disk)",
        file=sys.stderr,
    )
    versions = collect(existing, MIN_VERSION)
    if not versions:
        print("error: no matching versions found", file=sys.stderr)
        return 1

    rendered = render(versions)
    min_str = ".".join(str(n) for n in MIN_VERSION)
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
