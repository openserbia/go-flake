#!/usr/bin/env python3
"""Fetch upstream Go release metadata from go.dev and regenerate versions.nix.

Reads https://go.dev/dl/?mode=json&include=all, keeps stable releases at or
above --min-version that publish archive tarballs for all four supported
(os, arch) pairs, and writes their SHA256 sums into versions.nix.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

GO_DL_JSON = "https://go.dev/dl/?mode=json&include=all"

# (nix attr key, go.dev "os", go.dev "arch")
PLATFORMS = [
    ("linux-amd64",  "linux",  "amd64"),
    ("linux-arm64",  "linux",  "arm64"),
    ("darwin-amd64", "darwin", "amd64"),
    ("darwin-arm64", "darwin", "arm64"),
]

DEFAULT_MIN_VERSION = "1.2.2"


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse "go1.26.3" -> (1, 26, 3). Returns None for rc/beta or malformed tags."""
    if not tag.startswith("go"):
        return None
    rest = tag[2:]
    if any(c.isalpha() for c in rest):  # rejects 1.26rc1, 1.26beta1, etc.
        return None
    try:
        nums = [int(p) for p in rest.split(".")]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    if len(nums) != 3:
        return None
    return (nums[0], nums[1], nums[2])


def parse_min_version(s: str) -> tuple[int, int, int]:
    try:
        nums = [int(p) for p in s.split(".")]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad version {s!r}") from e
    while len(nums) < 3:
        nums.append(0)
    if len(nums) != 3:
        raise argparse.ArgumentTypeError(f"bad version {s!r}")
    return (nums[0], nums[1], nums[2])


def fetch_releases(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "go-flake-update"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def collect_versions(
    releases: list[dict],
    min_version: tuple[int, int, int],
) -> dict[tuple[int, int, int], dict[str, str]]:
    out: dict[tuple[int, int, int], dict[str, str]] = {}
    for release in releases:
        if not release.get("stable", False):
            continue
        v = parse_version(release.get("version", ""))
        if v is None or v < min_version:
            continue
        sums: dict[str, str] = {}
        for f in release.get("files", []):
            if f.get("kind") != "archive":
                continue
            for nix_key, os_, arch in PLATFORMS:
                if f.get("os") == os_ and f.get("arch") == arch:
                    sums[nix_key] = f["sha256"]
                    break
        if len(sums) == len(PLATFORMS):
            out[v] = sums
    return out


def render_nix(versions: dict[tuple[int, int, int], dict[str, str]]) -> str:
    # Align '=' to the widest quoted key (e.g. "darwin-amd64") plus one space.
    key_field = max(len(k) for k, _, _ in PLATFORMS) + 3  # 2 quotes + 1 trailing space

    lines = ["{"]
    for v in sorted(versions):
        ver = ".".join(str(n) for n in v)
        lines.append(f'  "{ver}" = {{')
        for nix_key, _, _ in PLATFORMS:
            quoted = f'"{nix_key}"'
            lines.append(f'    {quoted:<{key_field}}= "{versions[v][nix_key]}";')
        lines.append("  };")
    lines.append("}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--min-version",
        type=parse_min_version,
        default=parse_min_version(DEFAULT_MIN_VERSION),
        help=f"Lowest Go version to include (default: {DEFAULT_MIN_VERSION})",
    )
    ap.add_argument(
        "-o", "--output",
        default=str(repo_root / "versions.nix"),
        help="Output path (default: <repo>/versions.nix)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if output would differ from existing file (no write)",
    )
    args = ap.parse_args()

    print(f"fetching {GO_DL_JSON}", file=sys.stderr)
    releases = fetch_releases(GO_DL_JSON)
    versions = collect_versions(releases, args.min_version)
    if not versions:
        print("error: no matching versions found", file=sys.stderr)
        return 1

    rendered = render_nix(versions)
    out_path = Path(args.output)

    min_str = ".".join(str(n) for n in args.min_version)
    print(f"collected {len(versions)} versions (>= {min_str})", file=sys.stderr)

    if args.check:
        existing = out_path.read_text() if out_path.exists() else ""
        if existing != rendered:
            print(f"{out_path} is out of date", file=sys.stderr)
            return 1
        print(f"{out_path} is up to date", file=sys.stderr)
        return 0

    out_path.write_text(rendered)
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
