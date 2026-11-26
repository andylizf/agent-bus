#!/usr/bin/env python3
"""Publish the read-only B300 board snapshot to GitHub Pages."""

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
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


REMOTE = "https://github.com/andylizf/b300-board.git"
REF = "refs/heads/gh-pages"
INTERVAL = 600
ET = ZoneInfo("America/New_York")
ROBOTS = '<meta name="robots" content="noindex, nofollow, noarchive">'

COLLECT = r'''
import grp
import json
import os
import pwd
import runpy
import sys
from functools import lru_cache
from pathlib import Path

root = Path('/scratch/gpfs/TRIDAO')
v2 = root / 'agent-bus-v2'
old = root / 'agent-bus'
lab_gid = grp.getgrnam('tridao').gr_gid
lib = (Path.home() / '.local/bin/bus').resolve().parents[1] / 'lib'
conf = {}
if (lib / 'agentbus/page.py').is_file():
    sys.path.insert(0, str(lib))
    from agentbus import core, page
    conf = core.load_conf()

@lru_cache(maxsize=None)
def lab_user(user):
    if not user:
        return None
    try:
        return user if lab_gid in os.getgrouplist(user, pwd.getpwnam(user).pw_gid) else None
    except KeyError:
        return None

def task(row, key):
    return dict(user=lab_user(row.get('user')), gpus=row['gpus'], note=row.get(key))

def booking(row):
    return dict(task(row, 'note'), start=row['start'], end=row['end'], state=row.get('state'))

if conf.get('path') == str(v2) and (v2 / 'config').is_file():
    # Use the installed page's collection and read its receipt cache without updating it.
    slots = (core.read_private(str(Path(core.STATE()) / 'slots.json')) or {}).get('slots', {})
    page.bk.seen_update = lambda receipts: slots
    data = page.collect(conf)
    if data['errors'] or data.get('identity') != 'ok':
        sys.exit('v2 board collection failed')
    result = dict(source='v2', stamp=data['t'], gpus=[dict(gpu=g['gpu'], util=g['util'], users=list(map(lab_user, g['procs'])))
                  for g in data['gpus']], entries=[task(e, 'note') for e in data['entries']],
                  bookings=[booking(b) for b in data['bookings'] if b['state'] != 'unpublished'])
else:
    if not (old / 'tools/board_page.py').is_file():
        sys.exit('no board renderer installed')
    legacy = runpy.run_path(str(old / 'tools/board_page.py'))
    data = legacy['collect'](str(old))
    result = dict(source='legacy', stamp=data['now'], gpus=[dict(gpu=g['gpu'], util=g['util'],
                  users=[lab_user(p['user']) for p in g['procs']]) for g in data['gpus']],
                  entries=[task(e, 'task') for e in data['entries']], bookings=[booking(b) for b in data['bookings']])

# Only these fields leave the GPU host. Diagnostics stay on stderr.
print(json.dumps(result))
'''

# Notes are free text. Withhold the whole note if it resembles a path, network address,
# credential, command invocation or pasted identifier. All displayed notes are escaped as text.
PRIVATE_TEXT = re.compile(
    r"[/\\=@`|$<>;&]|\b(?:[a-z][a-z0-9_-]*\.)+(?:[a-z]{2,}|local)\b|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:[\da-f]{0,4}:){2,}[\da-f]{0,4}|"
    r"\b(?:token|secret|credentials?|bearer|password|passwd|passphrase|api[-_ ]?key|private[-_ ]?key)\b|"
    r"\b(?:cmd|command)\s*:|\b(?:srun|sbatch|curl|wget|scp|ssh|bash|sh|zsh|python[23]?|torchrun|uv run)\s+\S|"
    r"(?<!\w)--[a-z]|\b[a-z0-9_+.-]{24,}\b",
    re.IGNORECASE,
)
NETID = re.compile(r"[a-z][a-z0-9]{1,15}\Z")
BOOKING_STATES = {"standing", "pending", "conflicting", "objected", "over-budget", "lapsed", "invalid"}


def public_note(note):
    if not isinstance(note, str):
        return ""
    note = " ".join(note.split())
    return "[redacted]" if PRIVATE_TEXT.search(note) else note


def netid(value):
    return value if isinstance(value, str) and NETID.fullmatch(value) else ""


def collect(ssh_config):
    command = ["ssh", "-T", "-o", "ConnectTimeout=12", "-o", "PreferredAuthentications=publickey,keyboard-interactive"]
    if ssh_config:
        command.extend(["-F", str(ssh_config)])
    result = subprocess.run(command + ["della-tridao-ts", "python3 -B -"], input=COLLECT,
                            capture_output=True, text=True, timeout=90, start_new_session=True,
                            env={**os.environ, "SSH_ASKPASS_REQUIRE": "never"})
    if result.returncode:
        raise RuntimeError(f"SSH collection failed (exit {result.returncode})")
    return json.loads(result.stdout)


def eastern(stamp):
    return datetime.fromtimestamp(stamp, ET)


def table(headers, rows, empty):
    head = "".join(f'<th scope="col">{header}</th>' for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>' if body else empty


def public_tables(snapshot):
    gpus = sorted(snapshot["gpus"], key=lambda gpu: gpu["gpu"])
    if [gpu["gpu"] for gpu in gpus] != list(range(8)):
        raise RuntimeError("incomplete GPU sample")
    entries = sorted((netid(row["user"]), public_note(row["note"]), row["gpus"]) for row in snapshot["entries"])
    current = []
    for gpu in gpus:
        index, util = gpu["gpu"], gpu["util"]
        users = ", ".join(sorted(set(filter(None, map(netid, gpu["users"])))))
        notes = sorted({f"{user}: {note}" if note else user for user, note, indices in entries if user and index in indices})
        utilization = f"{util}%" if type(util) is int and 0 <= util <= 100 else "Unknown"
        current.append((index, utilization, users, "; ".join(notes)))

    bookings = {}
    for row in sorted(snapshot["bookings"], key=lambda b: (b["start"], b["end"], netid(b["user"]))):
        if row["end"] <= snapshot["stamp"] or not netid(row["user"]):
            continue
        start, end = eastern(row["start"]), eastern(row["end"])
        indices = ", ".join(str(i) for i in sorted(set(row["gpus"]) & set(range(8))))
        state = row["state"] if row["state"] in BOOKING_STATES else ""
        bookings.setdefault(start.strftime("%A, %b %d"), []).append(
            (row["user"], indices, start.strftime("%H:%M"), end.strftime("%a %b %d %H:%M"), state, public_note(row["note"])))
    calendar = "".join(f"<h3>{day}</h3>" + table(("NetID", "GPUs", "Starts (ET)", "Ends (ET)", "Status", "Note"),
                                                rows, "") for day, rows in bookings.items())
    return table(("GPU", "Utilization", "Running users", "Board task notes"), current, ""), calendar or "<p>No bookings.</p>"


def render(stamp, current, calendar):
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{ROBOTS}
<title>B300 GPU board</title>
<style>
:root {{ color-scheme: light dark; --line: #ddd; --stale: #a04300; }}
@media (prefers-color-scheme: dark) {{ :root {{ --line: #454545; --stale: #ffb070; }} }}
body {{ margin: 1.25rem auto; padding: 0 1rem; max-width: 70rem; font: 15px/1.5 system-ui, sans-serif; }}
h1 {{ margin-bottom: .25rem; }} h2 {{ margin-top: 1.8rem; }} h3 {{ margin-bottom: .4rem; }}
table {{ width: 100%; border-collapse: collapse; }} .scroll {{ overflow-x: auto; }}
th, td {{ text-align: left; padding: .4rem .7rem; border-bottom: 1px solid var(--line); vertical-align: top; }}
.stale {{ color: var(--stale); font-weight: 700; }}
</style></head><body>
<h1>B300 GPU board</h1>
<div id="stamp">Data as of <time datetime="{eastern(stamp).isoformat()}">{eastern(stamp):%a %b %d, %H:%M} ET</time><span id="stale"></span></div>
<h2>Now on each GPU</h2>{current}
<h2>Booking calendar</h2>{calendar}
<script>
const stamp = document.getElementById('stamp');
const dataTime = Date.parse(stamp.querySelector('time').dateTime);
function showAge() {{
  const stale = Date.now() - dataTime >= 15 * 60 * 1000;
  stamp.classList.toggle('stale', stale);
  document.getElementById('stale').textContent = stale ? ' (stale)' : '';
}}
showAge();
setTimeout(showAge, Math.max(0, dataTime + 15 * 60 * 1000 - Date.now()));
</script></body></html>
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


def publish(state, digest, page, summary):
    repo = state / "pages.git"
    if not (repo / "HEAD").exists():
        git(repo, "init", "--bare")
    advertised = git(repo, "ls-remote", REMOTE, REF).split()
    previous = advertised[0] if advertised else ""
    if previous:
        git(repo, "fetch", "--no-tags", "--depth=1", REMOTE, REF)
        if git(repo, "show", "-s", "--format=%b", "FETCH_HEAD").strip() == f"content-sha256: {digest}":
            log(f"status=unchanged {summary}")
            return

    last_attempt = state / "last-attempt"
    remaining = INTERVAL - (time.time() - float(last_attempt.read_text())) if last_attempt.exists() else 0
    if remaining > 20:
        log(f"status=rate-limited {summary}")
        return
    if remaining > 0:
        time.sleep(remaining)
    index = git(repo, "hash-object", "-w", "--stdin", data=page).strip()
    nojekyll = git(repo, "hash-object", "-w", "--stdin", data="").strip()
    tree = git(repo, "mktree", data=f"100644 blob {nojekyll}\t.nojekyll\n100644 blob {index}\tindex.html\n").strip()
    commit = git(repo, "commit-tree", tree, "-m", "Publish board snapshot", "-m", f"content-sha256: {digest}").strip()
    last_attempt.write_text(str(time.time()))
    git(repo, "push", f"--force-with-lease={REF}:{previous}", REMOTE, f"{commit}:{REF}")
    log(f"status=published {summary} commit={commit}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-config", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print the public HTML without writing local state or pushing")
    args = parser.parse_args()
    if not args.dry_run and not args.state_dir:
        parser.error("--state-dir is required to publish")
    log("status=collecting")
    snapshot = collect(args.ssh_config)
    stamp, source = snapshot["stamp"], snapshot["source"]
    if abs(time.time() - stamp) >= 900:
        raise RuntimeError("sample time is outside the freshness window")
    current, calendar = public_tables(snapshot)
    page = render(stamp, current, calendar)
    summary = f"source={source} data_as_of={datetime.fromtimestamp(stamp, timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    if args.dry_run:
        log(f"status=rendered {summary}")
        sys.stdout.write(page)
        return
    state = args.state_dir
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        v2_seen = state / "v2-seen"
        if source == "v2":
            v2_seen.touch()
        elif v2_seen.exists():
            raise RuntimeError("v2 disappeared after cutover")
        digest = hashlib.sha256((source + render(0, current, calendar)).encode()).hexdigest()
        publish(state, digest, page, summary)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Command output may contain private board data. Log only our own error messages.
        log(f"status=failed error={exc if isinstance(exc, RuntimeError) else type(exc).__name__}")
        sys.exit(1)
