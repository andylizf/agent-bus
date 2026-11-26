#!/usr/bin/env python3
"""A stand-in for nvidia-smi driven by the JSON file named in $AGENT_BUS_TEST_GPUS:
{"fail": false, "gpus": [{"index": 0, "uuid": "GPU-0", "util": 0}, ...], "apps": [{"uuid": "GPU-0", "pid": 123}]}.
Answers --query-gpu=... and --query-compute-apps=... in csv,noheader,nounits form, and -q -d PIDS as a report. An app
with "type": "G" (a graphics-only process) appears only in the report, as on a real driver."""
import json, os, sys

spec = json.load(open(os.environ["AGENT_BUS_TEST_GPUS"]))
if spec.get("fail"):
    sys.exit(9)
q = next((a for a in sys.argv[1:] if a.startswith("--query")), "")
if q.startswith("--query-gpu="):
    fields = q.split("=", 1)[1].split(",")
    for g in spec["gpus"]:
        row = {"index": g["index"], "uuid": g["uuid"], "utilization.gpu": g.get("util", 0),
               "memory.used": g.get("mem", 0), "memory.total": 100000, "name": "Fake GPU",
               "pci.bus_id": f"00000000:{g['index'] + 16:02X}:00.0"}
        print(", ".join(str(row[f]) for f in fields))
elif q.startswith("--query-compute-apps="):
    fields = q.split("=", 1)[1].split(",")
    for a in spec.get("apps", []):
        if a.get("type", "C") == "G":
            continue
        row = {"gpu_uuid": a["uuid"], "pid": a["pid"], "used_gpu_memory": a.get("mem", 0)}
        print(", ".join(str(row[f]) for f in fields))
elif "-q" in sys.argv[1:]:
    if spec.get("pids_fail"):
        sys.exit(9)
    print(f"Attached GPUs : {len(spec['gpus'])}")
    for g in (g for g in spec["gpus"] if g["index"] not in spec.get("pids_missing", [])):
        print(f"GPU 00000000:{g['index'] + 16:02X}:00.0\n    Processes")
        for a in (a for a in spec.get("apps", []) if a["uuid"] == g["uuid"]):
            print(f"        Process ID : {a['pid']}\n            Type : {a.get('type', 'C')}\n"
                  f"            Used GPU Memory : {a.get('mem', 0)} MiB")
