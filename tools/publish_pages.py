#!/usr/bin/env python3
"""Build the public B300 board shell and publish its fallback snapshot to GitHub Pages."""

import argparse
import fcntl
import hashlib
import html
import subprocess
import sys
import time
from pathlib import Path

from board_feed import log, sample, stamp


SITE = "https://b300-board.github.io/"
FEED = "https://cc.zhifei.li/b300/board.json"
REMOTE = "https://github.com/b300-board/b300-board.github.io.git"
LEGACY_REMOTE = "https://github.com/andylizf/b300-board.git"
REF = "refs/heads/gh-pages"
INTERVAL = 360  # at most ten pushes per hour
ROBOTS = '<meta name="robots" content="noindex, nofollow, noarchive">'


def build_shell(dest):
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here.parent / "lib"))
    from agentbus.board_view import CSS, OBJECTION_WINDOW

    dest.mkdir(parents=True, exist_ok=True)
    index = (here / "board.html").read_text().replace("@FEED@", FEED).replace("@OBJECTION@", OBJECTION_WINDOW)
    for ext, content in (("css", CSS), ("js", (here / "board.js").read_text())):
        name = f"board-{hashlib.sha256(content.encode()).hexdigest()[:12]}.{ext}"
        (dest / name).write_text(content)
        index = index.replace(f"@{ext.upper()}@", name)
    (dest / "index.html").write_text(index)


def redirect(url):
    target = html.escape(url, quote=True)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{ROBOTS}
<meta http-equiv="refresh" content="0; url={target}">
<title>B300 GPU board</title></head><body>
<p><a href="{target}">Open the B300 GPU board</a></p>
</body></html>
'''


def git(repo, *args, data=None):
    result = subprocess.run(["git", "--git-dir", str(repo), "-c", "credential.helper=",
                             "-c", "credential.helper=!gh auth git-credential", *args],
                            input=data, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def publish(state, remote, files, digest, summary):
    repo = state / "pages.git"
    if not (repo / "HEAD").exists():
        git(repo, "init", "--bare")
    advertised = git(repo, "ls-remote", remote, REF).split()
    previous = advertised[0] if advertised else ""
    if previous:
        git(repo, "fetch", "--no-tags", "--depth=1", remote, REF)
        if git(repo, "show", "-s", "--format=%b", "FETCH_HEAD").strip() == f"content-sha256: {digest}":
            log(f"status=unchanged {summary}")
            return

    last_attempt = state / "last-attempt"
    remaining = INTERVAL - (time.time() - float(last_attempt.read_text())) if last_attempt.exists() else 0
    if remaining > 0:
        log(f"status=rate-limited wait_s={remaining:.0f} {summary}")
        return
    blobs = []
    for name, content in sorted(files.items()):
        blob = git(repo, "hash-object", "-w", "--stdin", data=content).strip()
        blobs.append(f"100644 blob {blob}\t{name}\n")
    tree = git(repo, "mktree", data="".join(blobs)).strip()
    commit = git(repo, "commit-tree", tree, "-m", "Publish B300 board", "-m", f"content-sha256: {digest}").strip()
    last_attempt.write_text(str(time.time()))
    git(repo, "push", f"--force-with-lease={REF}:{previous}", remote, f"{commit}:{REF}")
    log(f"status=published {summary} commit={commit}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-config", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--shell-dir", type=Path, default=Path(__file__).resolve().parent / "shell")
    parser.add_argument("--build-shell", type=Path, metavar="DIR", help="build the static shell without publishing")
    parser.add_argument("--dry-run", action="store_true", help="print index.html without publishing")
    parser.add_argument("--retire-legacy", action="store_true", help="point the former Pages site at the organization site")
    args = parser.parse_args()
    if args.build_shell:
        build_shell(args.build_shell)
        return
    if not args.dry_run and not args.state_dir:
        parser.error("--state-dir is required to publish")

    files = {".nojekyll": ""}
    if args.retire_legacy:
        remote, summary = LEGACY_REMOTE, "mode=legacy-redirect"
        files["index.html"] = redirect(SITE)
        state = args.state_dir
    else:
        index = (args.shell_dir / "index.html").read_text()
        files["index.html"] = index
        files["snapshot.html"] = index.replace(FEED, "./snapshot.json", 1)
        for path in args.shell_dir.glob("board-*"):
            if path.name in index:
                files[path.name] = path.read_text()
        if args.dry_run:
            summary = "mode=shell"
        else:
            log("status=collecting")
            files["snapshot.json"] = sample(args.ssh_config, args.state_dir, max_age=30)
            summary = f"mode=snapshot data_as_of={stamp(files['snapshot.json'])}"
        remote = REMOTE
        state = args.state_dir / "organization" if args.state_dir else None

    if args.dry_run:
        log(f"status=rendered {summary}")
        sys.stdout.write(files["index.html"])
        return
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(f"{name}\0{content}\0".encode())
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        publish(state, remote, files, digest.hexdigest(), summary)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        log(f"status=failed error={exc if isinstance(exc, RuntimeError) else type(exc).__name__}")
        sys.exit(1)
