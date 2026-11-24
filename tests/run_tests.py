#!/usr/bin/env python3
"""Run the v2 test suite, each test in its own process, N at a time, and print one line per test and a summary.
  python3 tests/run_tests.py [-j 4] [-k <substring>] [-o <log-dir>]
Exits with the number of failed or errored tests. Every test keeps its own board, state and unit prefix, so tests
run side by side without sharing anything."""
import argparse, os, subprocess, sys, time, unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.realpath(__file__))
V2 = os.path.join(HERE, "v2")


def ids(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from ids(t)
        else:
            yield t.id()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("-k", default="")
    ap.add_argument("-o", default=None)
    a = ap.parse_args()
    sys.path.insert(0, V2)
    tests = [t for t in ids(unittest.defaultTestLoader.discover(V2, pattern="test_*.py")) if a.k in t]
    if a.o:
        os.makedirs(a.o, exist_ok=True)
    results = {}

    def one(tid):
        t0 = time.time()
        p = subprocess.run([sys.executable, "-m", "unittest", tid], cwd=V2, capture_output=True, text=True)
        out = p.stdout + p.stderr
        last = [l for l in out.strip().splitlines() if l.strip()][-1:] or [""]
        status = "ok" if p.returncode == 0 and "skipped" not in last[0] else (
            "skip" if p.returncode == 0 else "FAIL")
        if a.o:
            with open(os.path.join(a.o, tid + ".log"), "w") as f:
                f.write(out)
        results[tid] = (status, time.time() - t0, out)
        print(f"{status:<4} {time.time() - t0:6.1f}s {tid}", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(a.j) as ex:
        list(ex.map(one, tests))
    bad = [t for t, r in results.items() if r[0] == "FAIL"]
    n = {s: sum(1 for r in results.values() if r[0] == s) for s in ("ok", "skip", "FAIL")}
    print(f"\n{len(tests)} tests in {time.time() - t0:.0f} s: {n['ok']} passed, {n['skip']} skipped, {n['FAIL']} failed")
    for t in bad:
        print(f"\n===== {t}\n" + "\n".join(results[t][2].strip().splitlines()[-25:]))
    return len(bad)


if __name__ == "__main__":
    sys.exit(main())
