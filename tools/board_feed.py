#!/usr/bin/env python3
"""Collect and publish the public B300 feed over SSH."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


INTERVAL = 15
COLLECT = r'''
import json
import sys
from pathlib import Path

lib = (Path.home() / '.local/bin/bus').resolve().parents[1] / 'lib'
sys.path.insert(0, str(lib))
from agentbus import core, page

data = page.collect(core.load_conf(), history=False)
boards = ('/scratch/gpfs/TRIDAO/agent-bus-v2', '/scratch/agent-bus-v2')
if data['board'] not in boards or [g['gpu'] for g in data['gpus'] if g['available']] != list(range(8)):
    sys.exit('incomplete board reading')
print(json.dumps(page.public_feed(data), separators=(',', ':')))
'''


def log(message):
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}", file=sys.stderr, flush=True)


def ssh(config, host, *args, payload, timeout):
    cmd = ["ssh", "-T"]
    if config:
        cmd += ["-F", str(config)]
    return subprocess.run(cmd + [host, *args], input=payload, capture_output=True, text=True,
                          timeout=timeout, check=True, start_new_session=True,
                          env={**os.environ, "SSH_ASKPASS_REQUIRE": "never"}).stdout


def atomic_write(path, text):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        name = tmp.name
        try:
            tmp.write(text)
            tmp.close()
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def sample(config, state, *, max_age=0):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "collect.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = state / "board.json"
        if max_age and cached.exists():
            payload = cached.read_text()
            if -2 <= time.time() - json.loads(payload)["t"] <= max_age:
                return payload
        payload = ssh(config, "della-tridao-ts", "python3 -IB -", payload=COLLECT, timeout=14)
        if not -2 <= time.time() - json.loads(payload)["t"] <= 20:
            raise ValueError("sample time is outside the freshness window")
        atomic_write(cached, payload)
        return payload


def stamp(payload):
    return datetime.fromtimestamp(json.loads(payload)["t"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--upload-host", default="ccs-b300")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    backoff = INTERVAL
    while True:
        started, stage = time.monotonic(), "collect"
        try:
            payload = sample(args.ssh_config, args.state_dir)
            stage = "upload"
            ssh(args.ssh_config, args.upload_host, payload=payload, timeout=8)
            log(f"status=updated data_as_of={stamp(payload)} elapsed_s={time.monotonic() - started:.2f}")
            if args.once:
                return 0
            time.sleep(max(0, INTERVAL - (time.monotonic() - started)))
            backoff = INTERVAL
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            detail = f" exit={exc.returncode}" if isinstance(exc, subprocess.CalledProcessError) else ""
            log(f"status=failed stage={stage} error={type(exc).__name__}{detail} retry_s={backoff}")
            if args.once:
                return 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


if __name__ == "__main__":
    sys.exit(main())
