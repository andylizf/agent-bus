#!/usr/bin/env python3
"""Publish the public B300 board snapshot and live relay to GitHub Pages."""

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


SITE = "https://b300-board.github.io/"
REMOTE = "https://github.com/b300-board/b300-board.github.io.git"
LEGACY_REMOTE = "https://github.com/andylizf/b300-board.git"
REF = "refs/heads/gh-pages"
INTERVAL = 360  # at most ten pushes per hour
ROBOTS = '<meta name="robots" content="noindex, nofollow, noarchive">'
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\Z")

# Send only the sanitized HTML, its change fingerprint, timestamp and public tunnel URL off the GPU host.
COLLECT = r'''
import hashlib
import json
import sys
from pathlib import Path

lib = (Path.home() / '.local/bin/bus').resolve().parents[1] / 'lib'
sys.path.insert(0, str(lib))
from agentbus import core, page

conf = core.load_conf()
if conf.get('path') != '/scratch/gpfs/TRIDAO/agent-bus-v2':
    sys.exit('unexpected board')
data = page.collect(conf)
if not data['board'] or [g['gpu'] for g in data['gpus'] if g['available']] != list(range(8)):
    sys.exit('incomplete board snapshot')
state = Path.home() / '.local/state/b300-board-tunnel/url'
url = state.read_text().strip() if state.exists() else ''
stamp = data['t']
body = page.render(data, snapshot=True)
# Include the renderer when checking for a change. Ignore the moving clock and timestamps used only in markup.
import agentbus.board_view
fingerprint = hashlib.sha256(Path(agentbus.board_view.__file__).read_bytes())
fingerprint.update(json.dumps({k: v for k, v in data.items() if k != 't'}, sort_keys=True).encode())
print(json.dumps(dict(stamp=stamp, snapshot=body, fingerprint=fingerprint.hexdigest(), url=url)))
'''


def collect(ssh_config):
    command = ["ssh", "-T", "-o", "ConnectTimeout=12", "-o", "PreferredAuthentications=publickey,keyboard-interactive"]
    if ssh_config:
        command.extend(["-F", str(ssh_config)])
    result = subprocess.run(command + ["della-tridao-ts", "python3 -IB -"], input=COLLECT,
                            capture_output=True, text=True, timeout=90, start_new_session=True,
                            env={**os.environ, "SSH_ASKPASS_REQUIRE": "never"})
    if result.returncode:
        raise RuntimeError(f"SSH collection failed (exit {result.returncode})")
    return json.loads(result.stdout)


def live_url(url):
    if not TUNNEL_URL.fullmatch(url):
        return ""
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            if (response.status == 200 and response.url.rstrip("/") == url
                    and response.headers.get("X-Robots-Tag") == "noindex, nofollow, noarchive"
                    and b"Hanken Grotesk" in response.read(16384)):
                return url
    except (OSError, urllib.error.HTTPError):
        pass
    return ""


def redirect(url, fallback=None):
    target = html.escape(url, quote=True)
    snapshot_link = f'<p><a href="{html.escape(fallback, quote=True)}">Open the latest snapshot</a></p>' if fallback else ""
    delay = 3 if fallback else 0
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{ROBOTS}
<meta http-equiv="refresh" content="{delay}; url={target}">
<title>B300 GPU board</title></head><body>
<p><a href="{target}">Open the B300 GPU board</a></p>
{snapshot_link}
</body></html>
'''

def log(message):
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}", file=sys.stderr, flush=True)


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
    parser.add_argument("--dry-run", action="store_true", help="print index.html without writing local state or pushing")
    parser.add_argument("--retire-legacy", action="store_true", help="point the former Pages site at the organization site")
    args = parser.parse_args()
    if not args.dry_run and not args.state_dir:
        parser.error("--state-dir is required to publish")

    files = {".nojekyll": ""}
    if args.retire_legacy:
        remote, summary = LEGACY_REMOTE, "mode=legacy-redirect"
        files["index.html"] = redirect(SITE)
        content = files["index.html"]
        state = args.state_dir
    else:
        log("status=collecting")
        snapshot = collect(args.ssh_config)
        stamp = snapshot["stamp"]
        if abs(time.time() - stamp) >= 900:
            raise RuntimeError("sample time is outside the freshness window")
        url = live_url(snapshot["url"])
        files["snapshot.html"] = snapshot["snapshot"]
        files["index.html"] = redirect(url, "snapshot.html") if url else files["snapshot.html"]
        content = "\n".join((snapshot["fingerprint"], files["index.html"] if url else "snapshot"))
        remote = REMOTE
        summary = f"mode={'relay' if url else 'snapshot'} data_as_of={datetime.fromtimestamp(stamp, timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
        state = args.state_dir / "organization" if args.state_dir else None

    if args.dry_run:
        log(f"status=rendered {summary}")
        sys.stdout.write(files["index.html"])
        return
    digest = hashlib.sha256(content.encode()).hexdigest()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        publish(state, remote, files, digest, summary)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        log(f"status=failed error={exc if isinstance(exc, RuntimeError) else type(exc).__name__}")
        sys.exit(1)
