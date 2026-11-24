#!/usr/bin/env python3
"""A stand-in for nvidia-smi driven by the JSON file named in $AGENT_BUS_TEST_GPUS:
{"fail": false, "gpus": [{"index": 0, "uuid": "GPU-0", "util": 0}, ...], "apps": [{"uuid": "GPU-0", "pid": 123}]}.
Answers the two queries agent-bus makes (--query-gpu=..., --query-compute-apps=...) in csv,noheader,nounits form."""
import json, os, sys

spec = json.load(open(os.environ["AGENT_BUS_TEST_GPUS"]))
if spec.get("fail"):
    sys.exit(9)
q = next((a for a in sys.argv[1:] if a.startswith("--query")), "")
if q.startswith("--query-gpu="):
    fields = q.split("=", 1)[1].split(",")
    for g in spec["gpus"]:
        row = {"index": g["index"], "uuid": g["uuid"], "utilization.gpu": g.get("util", 0),
               "memory.used": g.get("mem", 0), "memory.total": 100000, "name": "Fake GPU", "pci.bus_id": "0"}
        print(", ".join(str(row[f]) for f in fields))
elif q.startswith("--query-compute-apps="):
    for a in spec.get("apps", []):
        print(f"{a['uuid']}, {a['pid']}")
