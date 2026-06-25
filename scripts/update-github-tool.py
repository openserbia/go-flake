#!/usr/bin/env python3
"""Refresh a Go-tool data file from upstream GitHub releases.

Walks every asset published per release for the selected tool, extracts the
(os, arch) pair from the asset name, and writes a sparse per-version table of
SHA256 sums. Two source modes:
  - checksums: parse a published checksums text file (golangci-lint, goreleaser)
  - digest:    use the GitHub asset API's per-asset sha256 digest (gofumpt)

Windows assets are excluded (no native Nix). Honors $GITHUB_TOKEN if set,
raising the API quota from 60/hr to 5000/hr.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# A normalized (os, arch) extracted from an upstream asset filename.
# os is always lowercase ("linux" not "Linux"). arch is the upstream's own
# spelling (we don't fold armv7 <-> armv6l etc. — that mapping lives in the
# flake's per-tool nix-system table).
def _normalize_arch(arch: str) -> str:
    # Only fold trivial aliases that mean the same architecture, so all four
    # tools' data files use a single name for x86-64 and i386. Anything else
    # (armv6/armv6l/armv7/arm, mips variants, loong64, …) is left as-is.
    return {"x86_64": "amd64", "i386": "386"}.get(arch, arch)


@dataclass
class ToolConfig:
    repo: str
    min_version: str
    output: str
    # Regex applied to asset names. MUST yield named groups "os" and "arch".
    # Any asset that doesn't match (or that yields os == "windows") is skipped.
    asset_pattern: re.Pattern
    # "checksums": parse a separately-published checksums.txt-style file.
    # "digest":    read the per-asset "digest" field from the GitHub API.
    mode: str
    # checksums-mode only: builds the URL of the per-release checksums file.
    checksums_url: Callable[[str, str], str] | None = None


def golangci_checksums(tag: str, ver: str) -> str:
    return (
        "https://github.com/golangci/golangci-lint/releases/download/"
        f"{tag}/golangci-lint-{ver}-checksums.txt"
    )


def goreleaser_checksums(tag: str, ver: str) -> str:
    return f"https://github.com/goreleaser/goreleaser/releases/download/{tag}/checksums.txt"


def doclint_checksums(tag: str, ver: str) -> str:
    return f"https://github.com/openserbia/doclint/releases/download/{tag}/checksums.txt"


TOOLS: dict[str, ToolConfig] = {
    "golangci-lint": ToolConfig(
        repo="golangci/golangci-lint",
        min_version="2.0.0",
        output="golangci-lint-versions.nix",
        asset_pattern=re.compile(
            r"^golangci-lint-(?P<ver>[\d.]+)-(?P<os>[a-z]+)-(?P<arch>[a-z0-9]+)\.tar\.gz$"
        ),
        mode="checksums",
        checksums_url=golangci_checksums,
    ),
    "goreleaser": ToolConfig(
        repo="goreleaser/goreleaser",
        min_version="2.0.0",
        output="goreleaser-versions.nix",
        asset_pattern=re.compile(
            r"^goreleaser_(?P<os>Linux|Darwin)_(?P<arch>[A-Za-z0-9_]+)\.tar\.gz$"
        ),
        mode="checksums",
        checksums_url=goreleaser_checksums,
    ),
    # First-party: openserbia's own GoReleaser-published linter.
    "doclint": ToolConfig(
        repo="openserbia/doclint",
        min_version="0.1.0",
        output="doclint-versions.nix",
        asset_pattern=re.compile(
            r"^doclint_(?P<ver>[\d.]+)_(?P<os>[a-z]+)_(?P<arch>[a-z0-9]+)\.tar\.gz$"
        ),
        mode="checksums",
        checksums_url=doclint_checksums,
    ),
    "gofumpt": ToolConfig(
        repo="mvdan/gofumpt",
        # 0.9.0 is the floor where GitHub's per-asset digest field is
        # populated; older releases predate that feature so we can't pin
        # them without downloading and hashing each binary ourselves.
        min_version="0.9.0",
        output="gofumpt-versions.nix",
        # Bare binary, no extension. Windows assets end in `.exe` and naturally
        # don't match this pattern.
        asset_pattern=re.compile(
            r"^gofumpt_v(?P<ver>[\d.]+)_(?P<os>[a-z]+)_(?P<arch>[a-z0-9]+)$"
        ),
        mode="digest",
    ),
}


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse "v2.12.2" or "2.12.2" -> (2, 12, 2). Rejects pre-releases."""
    if tag.startswith("v"):
        tag = tag[1:]
    if any(c in tag for c in "-+"):  # 2.0.0-rc1, 2.0.0+meta
        return None
    parts = tag.split(".")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def parse_min_version(s: str) -> tuple[int, int, int]:
    v = parse_version(s)
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


# GitHub API responses to this runner intermittently drop mid-body
# (http.client.IncompleteRead) — large listings like golangci-lint's
# releases run ~1MB and a single bad TCP segment kills the whole run.
# Retry GETs on transient network errors and 5xx/429 statuses.
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF = (1, 2, 4)  # seconds before attempts 2, 3, 4
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


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "go-flake-update"})
    return _fetch_retry(req, lambda r: r.read().decode("utf-8"))


def list_releases(repo: str, max_pages: int = 2) -> list[dict]:
    # Only scan the latest few pages: combined with per_page=10 that's
    # ~20 recent releases, which a daily cron will always cover (no tool
    # publishes that fast). per_page=10 also keeps each chunk small —
    # goreleaser's full release list at per_page=100 is ~2MB and
    # intermittently triggers IncompleteRead from GitHub's API.
    per_page = 10
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        chunk = fetch_json(
            f"https://api.github.com/repos/{repo}/releases?per_page={per_page}&page={page}"
        )
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < per_page:
            break
    return out


# Output-file parser. The data file is the deterministic output of
# render_nix below, so a simple line scanner is sufficient — no need
# to pull in a real Nix parser.
NIX_VERSION_RE = re.compile(r'^\s*"(\d+\.\d+\.\d+)"\s*=\s*\{\s*$')
NIX_PLATFORM_RE = re.compile(r'^\s*"([^"]+)"\s*=\s*"([0-9a-f]{64})"\s*;\s*$')


def parse_existing_nix(text: str) -> dict[tuple[int, int, int], dict[str, str]]:
    """Recover the {version -> {platform -> sha}} map from a previously
    written -versions.nix file, so the updater can skip per-release work
    for versions already on disk."""
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
            out[cur_ver] = cur_map
            cur_ver = None
            cur_map = {}
            continue
        m = NIX_PLATFORM_RE.match(line)
        if m and cur_ver is not None:
            cur_map[m.group(1)] = m.group(2)
    return out


# sha256sum format: "<64-hex>  [*]<filename>"; the '*' marks binary-mode files.
CHECKSUM_RE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)\s*$", re.MULTILINE)


def parse_checksums(body: str) -> dict[str, str]:
    return {m.group(2): m.group(1) for m in CHECKSUM_RE.finditer(body)}


def _digest_to_sha256(digest: str | None) -> str | None:
    """GitHub returns digests as "sha256:<hex>"."""
    if not digest:
        return None
    if not digest.startswith("sha256:"):
        return None
    return digest[len("sha256:"):]


def _extract_platform(cfg: ToolConfig, asset_name: str) -> tuple[str, str] | None:
    m = cfg.asset_pattern.match(asset_name)
    if not m:
        return None
    os_ = m.group("os").lower()
    if os_ == "windows":
        return None
    return (os_, _normalize_arch(m.group("arch")))


def collect_versions(
    cfg: ToolConfig,
    min_version: tuple[int, int, int],
    existing: dict[tuple[int, int, int], dict[str, str]],
) -> dict[tuple[int, int, int], dict[str, str]]:
    # Start from existing data and only resolve versions that aren't
    # already on disk. Upstream tools don't republish a tag's binaries
    # under the same version, so an entry that exists is authoritative.
    out: dict[tuple[int, int, int], dict[str, str]] = {
        v: dict(m) for v, m in existing.items() if v >= min_version
    }
    releases = list_releases(cfg.repo)
    print(f"  {len(releases)} releases scanned (latest pages)", file=sys.stderr)
    for r in releases:
        if r.get("draft") or r.get("prerelease"):
            continue
        v = parse_version(r.get("tag_name", ""))
        if v is None or v < min_version:
            continue
        if v in out:
            continue
        ver_str = ".".join(str(n) for n in v)

        per_platform: dict[str, str] = {}

        if cfg.mode == "checksums":
            assert cfg.checksums_url is not None
            url = cfg.checksums_url(r["tag_name"], ver_str)
            try:
                body = fetch_text(url)
            except urllib.error.HTTPError as e:
                print(f"  skip {ver_str}: {url} -> HTTP {e.code}", file=sys.stderr)
                continue
            sums = parse_checksums(body)
            for asset_name, sha in sums.items():
                plat = _extract_platform(cfg, asset_name)
                if plat is None:
                    continue
                per_platform[f"{plat[0]}-{plat[1]}"] = sha
        elif cfg.mode == "digest":
            for a in r.get("assets", []):
                plat = _extract_platform(cfg, a.get("name", ""))
                if plat is None:
                    continue
                sha = _digest_to_sha256(a.get("digest"))
                if sha is None:
                    continue
                per_platform[f"{plat[0]}-{plat[1]}"] = sha
        else:
            raise ValueError(f"unknown mode {cfg.mode!r}")

        if per_platform:
            out[v] = per_platform
    return out


def render_nix(versions: dict[tuple[int, int, int], dict[str, str]]) -> str:
    all_keys = {k for sums in versions.values() for k in sums}
    if not all_keys:
        return "{\n}\n"
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
    ap.add_argument("--tool", choices=sorted(TOOLS), required=True)
    ap.add_argument(
        "--min-version", type=parse_min_version, default=None,
        help="Override the tool's default minimum version",
    )
    ap.add_argument(
        "-o", "--output", default=None,
        help="Override output path (default: <repo>/<tool>-versions.nix)",
    )
    ap.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if output would differ from existing file (no write). "
             "Hits the GitHub API to discover new releases.",
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

    existing = (
        parse_existing_nix(out_path.read_text()) if out_path.exists() else {}
    )

    if args.validate:
        if not out_path.exists():
            print(f"{out_path} does not exist", file=sys.stderr)
            return 1
        rendered = render_nix(existing)
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
        f"fetching releases for {cfg.repo} "
        f"({len(existing)} versions already on disk)",
        file=sys.stderr,
    )
    versions = collect_versions(cfg, min_ver, existing)
    if not versions:
        print("error: no matching versions found", file=sys.stderr)
        return 1

    rendered = render_nix(versions)
    min_str = ".".join(str(n) for n in min_ver)
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
