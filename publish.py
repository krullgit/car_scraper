#!/usr/bin/env python3
"""Publish cars.json (and status.json) to a public GitHub repository.

Pushes only the feed files into the repo so they are reachable at permanent
raw URLs (no login, no JavaScript):

    https://raw.githubusercontent.com/<OWNER>/<REPO>/main/cars.json
    https://raw.githubusercontent.com/<OWNER>/<REPO>/main/status.json

Credentials come only from the environment (never hard-coded):
    GITHUB_TOKEN    # PAT with minimal write scope for this repo (Contents: RW)
    GITHUB_OWNER    # e.g. 'krullgit'
    GITHUB_REPO     # e.g. 'car_scraper'
    (optional fallback: GH_TOKEN and CAR_FEED_REPO="owner/repo")

The repo is cloned into a local cache (<.feed_cache>) and pushed on `main`.

Usage:
    python3 publish.py                # build feed + status, push if changed
    python3 publish.py --no-build     # only push existing feed files
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import config
import feed


def env_any(*names: str) -> str:
    for n in names:
        val = os.environ.get(n, "").strip()
        if val:
            return val
    return ""


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


def git_remote_url(owner: str, repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def ensure_cache(owner: str, repo: str, token: str) -> Path:
    cache_dir = config.SCRIPT_DIR / ".feed_cache"
    remote_url = git_remote_url(owner, repo, token)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir / ".git").exists():
        print(f"  Cloning {owner}/{repo} ...")
        run(cache_dir, "git", "init", "-b", "main")
        run(cache_dir, "git", "config", "user.name", config.GIT_AUTHOR)
        run(cache_dir, "git", "config", "user.email", config.GIT_EMAIL)
        run(cache_dir, "git", "remote", "add", "origin", remote_url)
        run(cache_dir, "git", "fetch", "origin", "main", check=False)
        run(cache_dir, "git", "reset", "--hard", "origin/main", check=False)
    else:
        run(cache_dir, "git", "config", "user.name", config.GIT_AUTHOR)
        run(cache_dir, "git", "config", "user.email", config.GIT_EMAIL)
        # Always refresh the origin URL so a rotated/changed token is used.
        run(cache_dir, "git", "remote", "set-url", "origin", remote_url)
        run(cache_dir, "git", "fetch", "origin", "main", check=False)
        run(cache_dir, "git", "reset", "--hard", "origin/main", check=False)
    return cache_dir


def copy_file(src: Path, dst: Path) -> None:
    try:
        dst.write_bytes(src.read_bytes())
    except OSError as e:
        print(f"  ERROR writing {dst}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish split feed (index + per-vehicle files) to GitHub")
    parser.add_argument("--no-build", action="store_true",
                        help="Skip rebuilding the feed; only push existing files")
    args = parser.parse_args()

    token = env_any("GITHUB_TOKEN", "GH_TOKEN")
    owner = env_any("GITHUB_OWNER") or config.DEFAULT_OWNER
    repo = env_any("GITHUB_REPO") or config.DEFAULT_REPO
    if not token:
        print("  Nothing published. Set GITHUB_TOKEN to enable.")
        sys.exit(0)
    print(f"  Publishing to github.com/{owner}/{repo}")

    if not args.no_build:
        conn = sqlite3.connect(str(config.DB_PATH))
        try:
            feed_summary, index = feed.write_split_feed(conn)
            status_data = feed.build_status(feed_summary, conn)
        finally:
            conn.close()
        feed.write_json(status_data, config.STATUS_FILE)
        print(f"  Built split feed: {feed_summary['vehicle_count']} vehicles "
              f"({len(index)} index entries)")

    if not config.INDEX_FILE.exists():
        print(f"  ERROR: {config.INDEX_FILE} not found", file=sys.stderr)
        sys.exit(1)

    cache = ensure_cache(owner, repo, token)

    # Copy index + status files.
    for src in (config.INDEX_FILE, config.STATUS_FILE):
        if src.exists():
            copy_file(src, cache / src.name)

    # Copy the whole per-vehicle detail directory.
    if config.CARS_DIR.exists():
        dest_dir = cache / "cars"
        import shutil
        shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in config.CARS_DIR.glob("*.json"):
            copy_file(f, dest_dir / f.name)

    # Determine if anything changed (index/status/cars dir).
    changed = run(cache, "git", "status", "--porcelain", check=False).stdout.strip() != ""
    if not changed:
        print("  No changes — nothing to push.")
        sys.exit(0)

    run(cache, "git", "add", "-A")
    run(cache, "git", "commit", "-m", "Update feed: cars_index.json + cars/*.json + status.json", check=False)
    push = run(cache, "git", "push", "origin", "main", check=False)
    if push.returncode != 0:
        run(cache, "git", "pull", "--rebase", "origin", "main", check=False)
        push = run(cache, "git", "push", "origin", "main", check=False)
    if push.returncode == 0:
        print(f"  Pushed split feed to github.com/{owner}/{repo}")
        print(f"  Index:   https://raw.githubusercontent.com/{owner}/{repo}/main/cars_index.json")
        print(f"  Details: https://raw.githubusercontent.com/{owner}/{repo}/main/cars/<id>.json")
    else:
        print(f"  ERROR: push failed\n{push.stderr}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()