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

The repo is cloned into a local cache (<_.feed_cache>) and pushed on `main`.

Usage:
    python3 publish_feed.py                # build feed + status, push if changed
    python3 publish_feed.py --no-build     # only push existing feed files
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FEED_FILE = SCRIPT_DIR / "cars.json"
STATUS_FILE = SCRIPT_DIR / "status.json"
CACHE_DIR = SCRIPT_DIR / ".feed_cache"

# Fallback repo (owner/repo) used only if env vars are not set.
DEFAULT_OWNER = "krullgit"
DEFAULT_REPO = "car_scraper"

GIT_AUTHOR = "car-feed-bot"
GIT_EMAIL = "car-feed-bot@users.noreply.github.com"


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
    remote_url = git_remote_url(owner, repo, token)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not (CACHE_DIR / ".git").exists():
        print(f"  Cloning {owner}/{repo} ...")
        run(CACHE_DIR, "git", "init", "-b", "main")
        run(CACHE_DIR, "git", "config", "user.name", GIT_AUTHOR)
        run(CACHE_DIR, "git", "config", "user.email", GIT_EMAIL)
        run(CACHE_DIR, "git", "remote", "add", "origin", remote_url)
        run(CACHE_DIR, "git", "fetch", "origin", "main", check=False)
        run(CACHE_DIR, "git", "reset", "--hard", "origin/main", check=False)
    else:
        run(CACHE_DIR, "git", "config", "user.name", GIT_AUTHOR)
        run(CACHE_DIR, "git", "config", "user.email", GIT_EMAIL)
        # Always refresh the origin URL so a rotated/changed token is used.
        run(CACHE_DIR, "git", "remote", "set-url", "origin", remote_url)
        run(CACHE_DIR, "git", "fetch", "origin", "main", check=False)
        run(CACHE_DIR, "git", "reset", "--hard", "origin/main", check=False)
    return CACHE_DIR


def copy_file(src: Path, dst: Path) -> None:
    try:
        dst.write_bytes(src.read_bytes())
    except OSError as e:
        print(f"  ERROR writing {dst}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish cars.json/status.json to GitHub")
    parser.add_argument("--no-build", action="store_true",
                        help="Skip rebuilding the feed; only push existing files")
    args = parser.parse_args()

    token = env_any("GITHUB_TOKEN", "GH_TOKEN")
    owner = env_any("GITHUB_OWNER") or DEFAULT_OWNER
    repo = env_any("GITHUB_REPO") or DEFAULT_REPO
    if not token:
        print("  Nothing published. Set GITHUB_TOKEN to enable.")
        sys.exit(0)
    print(f"  Publishing to github.com/{owner}/{repo}")

    if not args.no_build:
        import feed
        conn = __import__("sqlite3").connect(str(feed.DB_PATH))
        try:
            f = feed.build_feed(conn)
        finally:
            conn.close()
        feed.write_feed(f, FEED_FILE)
        feed.write_json(feed.build_status(f), STATUS_FILE)
        print(f"  Built feed: {len(f['vehicles'])} vehicles")

    both_exist = FEED_FILE.exists() and STATUS_FILE.exists()
    files = [FEED_FILE, STATUS_FILE] if both_exist else [FEED_FILE] if FEED_FILE.exists() else []
    if not files:
        print(f"  ERROR: {FEED_FILE} not found", file=sys.stderr)
        sys.exit(1)

    cache = ensure_cache(owner, repo, token)
    for src in files:
        copy_file(src, cache / src.name)

    # Determine if anything changed. Untracked files (fresh repo, first push)
    # are not visible to `git diff`, so check porcelain status as well.
    has_head = run(cache, "git", "rev-parse", "--verify", "HEAD", check=False).returncode == 0
    changed = (not has_head) or any(
        run(cache, "git", "diff", "--quiet", "--", src.name, check=False).returncode != 0
        for src in files
    )
    if not changed:
        porcelain = run(cache, "git", "status", "--porcelain", check=False).stdout
        changed = any(f" {src.name}" in porcelain or f"{src.name}" in porcelain for src in files)
    if not changed:
        print("  No changes — nothing to push.")
        sys.exit(0)

    run(cache, "git", "add", *(src.name for src in files))
    run(cache, "git", "commit", "-m", "Update feed: cars.json + status.json", check=False)
    push = run(cache, "git", "push", "origin", "main", check=False)
    if push.returncode != 0:
        run(cache, "git", "pull", "--rebase", "origin", "main", check=False)
        push = run(cache, "git", "push", "origin", "main", check=False)
    if push.returncode == 0:
        print(f"  Pushed feed to github.com/{owner}/{repo}")
        print(f"  Raw URL: https://raw.githubusercontent.com/{owner}/{repo}/main/cars.json")
    else:
        print(f"  ERROR: push failed\n{push.stderr}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()