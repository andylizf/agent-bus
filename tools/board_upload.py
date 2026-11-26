#!/usr/bin/python3
"""Receive board.json as stdin from the upload-only SSH key."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path


DEST = Path("/srv/b300-board/board.json")
MAX_BYTES = 512 * 1024


def main():
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        sys.exit("feed too large")
    t = json.loads(data)["t"]
    if type(t) is not int or abs(time.time() - t) > 30:
        sys.exit("feed timestamp out of range")
    with tempfile.NamedTemporaryFile(dir=DEST.parent, delete=False) as f:
        path = Path(f.name)
        try:
            f.write(data)
            f.close()
            path.chmod(0o644)
            path.replace(DEST)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
