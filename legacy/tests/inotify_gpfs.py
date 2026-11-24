#!/usr/bin/env python3
"""Does inotify on one node see files created in a shared (GPFS/NFS) directory by another node?

Usage: inotify_gpfs.py <dir> <seconds>   (prints one line per create / close-write / move-in event it sees)
"""
import ctypes, os, select, struct, sys, time

d, secs = sys.argv[1], float(sys.argv[2])
libc = ctypes.CDLL("libc.so.6", use_errno=True)
fd = libc.inotify_init1(os.O_NONBLOCK)
wd = libc.inotify_add_watch(fd, d.encode(), 0x100 | 0x80 | 0x8)  # IN_CREATE | IN_MOVED_TO | IN_CLOSE_WRITE
print(f"{time.strftime('%H:%M:%S')} watching {d} on {os.uname().nodename} wd={wd}", flush=True)
end = time.time() + secs
while time.time() < end:
    r, _, _ = select.select([fd], [], [], 0.5)
    if r:
        buf = os.read(fd, 4096)
        i = 0
        while i < len(buf):
            _, mask, _, ln = struct.unpack_from("iIII", buf, i)
            name = buf[i + 16:i + 16 + ln].rstrip(b"\0").decode()
            print(f"{time.strftime('%H:%M:%S')} event mask={mask:#x} {name}", flush=True)
            i += 16 + ln
print(f"{time.strftime('%H:%M:%S')} done", flush=True)
