#!/usr/bin/env python3
"""Expose the loopback B300 page and publish the connected Quick Tunnel URL locally."""

import os
import re
import subprocess
import sys
from pathlib import Path


def run(state):
    url_file = state / "url"
    candidate = state / "url.new"
    url = ""
    connected = False
    command = [str(Path.home() / ".local/bin/cloudflared"), "tunnel", "--no-autoupdate",
               "--url", "http://127.0.0.1:8300"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
        for line in child.stdout:
            print(line, end="", flush=True)
            if match := re.search(r"https://[a-z0-9-]+\.trycloudflare\.com\b", line):
                url = match.group()
            if "Registered tunnel connection" in line:
                connected = True
            if connected and url and not url_file.exists():
                candidate.write_text(url + "\n")
                os.replace(candidate, url_file)
        return child.wait()


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])))
