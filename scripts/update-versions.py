#!/usr/bin/env python3
"""Fetch upstream Go release metadata from go.dev and regenerate versions.nix.

Reads https://go.dev/dl/?mode=json&include=all and writes a sparse per-version
table of SHA256 sums covering every (os, arch) tarball upstream publishes.
Windows is excluded (no native Nix); known non-OS sentinels are filtered out.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

GO_DL_JSON = "https://go.dev/dl/?mode=json&include=all"

# OSes for which we mirror archives. Windows is dropped (no native Nix).
# Anything else upstream may publish (bootstrap, wasm, etc.) is silently
# filtered out because its f["os"] isn't in this set.
KNOWN_OSES = {
    "linux", "darwin",
    "freebsd", "netbsd", "openbsd", "dragonfly",
    "illumos", "solaris", "aix", "plan9",
}

DEFAULT_MIN_VERSION = "1.2.2"


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse "go1.26.5" -> (1, 26, 5). Returns None for rc/beta or malformed tags."""
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
            os_ = f.get("os")
            arch = f.get("arch")
            if os_ not in KNOWN_OSES or not arch:
                continue
            # go.dev's JSON omits sha256 for very old releases (pre-1.5-ish)
            # — skip those entries rather than emitting an empty pin.
            sha = f.get("sha256")
            if not sha:
                continue
            sums[f"{os_}-{arch}"] = sha
        if sums:
            out[v] = sums
    return out


# Output-file parser. Mirrors update-github-tool.py's NIX_*_RE — same data
# shape (version -> platform -> sha256-hex). Used by --validate to verify
# versions.nix round-trips through parse + render without diffs.
NIX_VERSION_RE = re.compile(r'^\s*"(\d+\.\d+\.\d+)"\s*=\s*\{\s*$')
NIX_PLATFORM_RE = re.compile(r'^\s*"([^"]+)"\s*=\s*"([0-9a-f]{64})"\s*;\s*$')


def parse_existing_nix(text: str) -> dict[tuple[int, int, int], dict[str, str]]:
    out: dict[tuple[int, int, int], dict[str, str]] = {}
    cur_ver: tuple[int, int, int] | None = None
    cur_map: dict[str, str] = {}
    for line in text.splitlines():
        m = NIX_VERSION_RE.match(line)
        if m:
            parts = m.group(1).split(".")
            try:
                v = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                continue
            cur_ver = v
            cur_map = {}
            continue
        if cur_ver is not None and line.strip() == "};":
            out[cur_ver] = cur_map
            cur_ver = None
            cur_map = {}
            continue
        m = NIX_PLATFORM_RE.match(line)
        if m and cur_ver is not None:
            cur_map[m.group(1)] = m.group(2)
    return out


def render_nix(versions: dict[tuple[int, int, int], dict[str, str]]) -> str:
    all_keys = {k for sums in versions.values() for k in sums}
    if not all_keys:
        return "{\n}\n"
    # Align '=' to the widest quoted key across the whole file plus one space.
    key_field = max(len(k) for k in all_keys) + 3  # 2 quotes + 1 trailing space

    lines = ["{"]
    for v in sorted(versions):
        ver = ".".join(str(n) for n in v)
        lines.append(f'  "{ver}" = {{')
        for key in sorted(versions[v]):
            quoted = f'"{key}"'
            lines.append(f'    {quoted:<{key_field}}= "{versions[v][key]}";')
        lines.append("  };")
    lines.append("}")
    lines.append("")
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
        help="Exit non-zero if output would differ from existing file (no write). "
             "Hits go.dev to discover new releases.",
    )
    ap.add_argument(
        "--validate",
        action="store_true",
        help="Exit non-zero if the existing data file isn't round-trip stable "
             "(parse + render produces different bytes). No network. Use in CI.",
    )
    args = ap.parse_args()

    out_path = Path(args.output)

    if args.validate:
        if not out_path.exists():
            print(f"{out_path} does not exist", file=sys.stderr)
            return 1
        cur = out_path.read_text()
        existing = parse_existing_nix(cur)
        rendered = render_nix(existing)
        if cur != rendered:
            print(f"{out_path} is not round-trip stable", file=sys.stderr)
            return 1
        print(
            f"{out_path} round-trips cleanly ({len(existing)} versions)",
            file=sys.stderr,
        )
        return 0

    print(f"fetching {GO_DL_JSON}", file=sys.stderr)
    releases = fetch_releases(GO_DL_JSON)
    versions = collect_versions(releases, args.min_version)
    if not versions:
        print("error: no matching versions found", file=sys.stderr)
        return 1

    rendered = render_nix(versions)

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
