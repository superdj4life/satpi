#!/usr/bin/env python3
# satpi
# Updates the satpi installation by pulling the latest code from the remote git repository.
# Preserves config/config.ini and any uncommitted local changes (stash/unstash).
# Reloads systemd units if generated unit files changed during the update.
# Author: Andreas Horvath / superdj4life
# Project: Autonomous, Config-driven satellite reception pipeline for Raspberry Pi

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("satpi.update_satpi")


def setup_logging(log_dir: Path | None):
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "update_satpi.log")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)


def run(cmd: list[str], cwd: Path, check=True) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout.strip():
        logger.debug("stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        (logger.debug if result.returncode == 0 else logger.error)("stderr: %s", result.stderr.strip())
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def git(args: list[str], cwd: Path, check=True) -> subprocess.CompletedProcess:
    return run(["git"] + args, cwd=cwd, check=check)


def has_uncommitted_changes(repo: Path) -> bool:
    # Only tracked files — untracked files don't block a merge and gitignored
    # files (like config.ini) are safe to leave in place.
    result = git(["diff", "--quiet", "HEAD"], repo, check=False)
    return result.returncode != 0


def stash(repo: Path) -> bool:
    result = git(["stash", "push", "-m", "update_satpi auto-stash"], repo)
    return "No local changes to save" not in result.stdout


def unstash(repo: Path):
    git(["stash", "pop"], repo)


def get_current_commit(repo: Path) -> str:
    return git(["rev-parse", "HEAD"], repo).stdout.strip()


def fetch(repo: Path, remote: str):
    logger.info("Fetching from %s...", remote)
    git(["fetch", remote], repo)


def merge(repo: Path, remote: str, branch: str) -> subprocess.CompletedProcess:
    logger.info("Merging %s/%s...", remote, branch)
    return git(["merge", f"{remote}/{branch}"], repo, check=False)


def get_changed_files(repo: Path, old_commit: str) -> list[str]:
    result = git(["diff", "--name-only", old_commit, "HEAD"], repo)
    return [f for f in result.stdout.strip().splitlines() if f]


def reload_systemd_units(repo: Path):
    units_dir = repo / "systemd" / "generated"
    if not units_dir.exists():
        return
    logger.info("Reloading systemd daemon...")
    result = run(["systemctl", "--user", "daemon-reload"], repo, check=False)
    if result.returncode != 0:
        logger.warning("systemctl daemon-reload failed: %s", result.stderr.strip())
    else:
        logger.info("systemd daemon reloaded.")


def main():
    parser = argparse.ArgumentParser(description="Update satpi from git remote")
    parser.add_argument("--remote", default="origin", help="Git remote to pull from (default: origin)")
    parser.add_argument("--branch", default="main", help="Branch to merge (default: main)")
    parser.add_argument("--log-dir", default=None, help="Directory to write update log (default: no file log)")
    parser.add_argument("--no-systemd-reload", action="store_true", help="Skip systemd daemon-reload after update")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    log_dir = Path(args.log_dir) if args.log_dir else None
    setup_logging(log_dir)

    logger.info("satpi updater — repo: %s", repo)

    config_file = repo / "config" / "config.ini"
    if config_file.exists():
        logger.info("config/config.ini present — will be preserved (it is gitignored).")

    stashed = False
    if has_uncommitted_changes(repo):
        logger.info("Stashing uncommitted local changes...")
        stashed = stash(repo)
        if stashed:
            logger.info("Local changes stashed.")
        else:
            logger.info("Nothing to stash.")

    old_commit = get_current_commit(repo)

    fetch(repo, args.remote)

    units_before = set()
    units_dir = repo / "systemd" / "generated"
    if units_dir.exists():
        units_before = {p.name for p in units_dir.iterdir()}

    result = merge(repo, args.remote, args.branch)

    if result.returncode != 0:
        logger.error("Merge failed — conflicts must be resolved manually:")
        logger.error(result.stdout.strip())
        logger.error(result.stderr.strip())
        if stashed:
            logger.warning("Your local changes are still in the stash (git stash pop to restore).")
        return 1

    new_commit = get_current_commit(repo)

    if old_commit == new_commit:
        logger.info("Already up to date.")
    else:
        changed = get_changed_files(repo, old_commit)
        logger.info("Updated %s -> %s", old_commit[:8], new_commit[:8])
        logger.info("Changed files (%d):", len(changed))
        for f in changed:
            logger.info("  %s", f)

        systemd_changed = any(f.startswith("systemd/generated/") for f in changed)
        if not args.no_systemd_reload and systemd_changed:
            reload_systemd_units(repo)

    if stashed:
        logger.info("Restoring stashed local changes...")
        try:
            unstash(repo)
            logger.info("Local changes restored.")
        except subprocess.CalledProcessError:
            logger.error("Stash pop failed — resolve conflicts manually with: git stash pop")
            return 1

    logger.info("Update complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
