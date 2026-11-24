"""Checks only the GPU host can run (AGENT_BUS_TEST_REAL_SMI=1 to enable): the real nvidia-smi view; an Apptainer
instance started inside a job found as the job's and stopped by its release (AGENT_BUS_TEST_SIF=<image>); and, only
with AGENT_BUS_TEST_GPU=<index> naming a GPU with no compute process on it, a trivial CUDA context (driver API through
ctypes, no framework) counted as the job's and released. The CUDA test refuses a GPU anyone is using."""
import json, os, subprocess, time, unittest

from helpers import BusTest, need_systemd, wait_for
from agentbus import core, runner

GPU = os.environ.get("AGENT_BUS_TEST_GPU")
SIF = os.environ.get("AGENT_BUS_TEST_SIF")
CUDA = ("python3 -c \"import ctypes,time; c=ctypes.CDLL('libcuda.so.1'); assert c.cuInit(0)==0; d=ctypes.c_int(); "
        "assert c.cuDeviceGet(ctypes.byref(d),0)==0; x=ctypes.c_void_p(); "
        "assert c.cuCtxCreate_v2(ctypes.byref(x),0,d)==0; print('ctx', flush=True); time.sleep(300)\"")


@unittest.skipUnless(os.environ.get("AGENT_BUS_TEST_REAL_SMI"), "set AGENT_BUS_TEST_REAL_SMI=1 on the GPU host")
@need_systemd
class RealGPU(BusTest):
    def setUp(self):
        super().setUp()
        self.env["AGENT_BUS_NVIDIA_SMI"] = os.environ["AGENT_BUS_NVIDIA_SMI"] = "nvidia-smi"

    def test_real_nvidia_view_parses(self):
        ok, idx, apps, util = runner.nvidia_view()
        self.assertTrue(ok)
        self.assertEqual(sorted(idx), list(range(len(idx))))
        self.assertTrue(all(u.startswith("GPU-") for u in idx.values()))

    @unittest.skipUnless(GPU, "set AGENT_BUS_TEST_GPU to a GPU index with no compute process on it")
    def test_cuda_context_counted_as_job_then_released(self):
        g = int(GPU)
        ok, idx, apps, _ = runner.nvidia_view()
        busy = [pid for uuid, pid in apps if uuid == idx.get(g)]
        if busy:
            self.skipTest(f"GPU {g} has compute processes {busy}: not free, not used")
        d = self.start(CUDA, str(g))
        log = self.state(d["id"])["log"]
        wait_for(lambda: "ctx" in open(log).read(), 60, what="the CUDA context")
        cg = self.state(d["id"])["units"]["cgroup_path"]
        clear, mine, other, err = runner.gpu_check([g], runner.in_cgroup_fn(cg, os.getuid()))
        self.assertIsNone(err)
        self.assertFalse(clear)
        self.assertEqual(len(mine), 1, (mine, other))
        r = self.bus_json("release", d["id"], "--wait", "60", "--json")
        self.assertTrue(r["released"], r)
        clear, mine, other, err = runner.gpu_check([g], runner.in_cgroup_fn(cg, os.getuid()))
        self.assertTrue(clear)
        print(f"\n[real GPU {g}] job pid on GPU {self.state(d['id']).get('id')} released; others on it: {other}")

    @unittest.skipUnless(SIF, "set AGENT_BUS_TEST_SIF to an Apptainer image")
    def test_apptainer_instance_stays_in_slice(self):
        name = f"abt{os.getpid()}"
        d = self.start(f"apptainer instance start --no-mount home,cwd {SIF} {name} && sleep 300", "0")
        pid = wait_for(lambda: next((int(i["pid"]) for i in json.loads(subprocess.run(
            ["apptainer", "instance", "list", "--json"], capture_output=True, text=True).stdout or "{}")
            .get("instances", []) if i["instance"] == name), None), 60, what="the instance")
        cg = core.proc_cgroup(pid)
        st = self.state(d["id"])
        inside = core.unit_name(d["id"]) + ".slice" in cg
        print(f"\n[apptainer] instance pid {pid} cgroup {cg}", file=__import__("sys").stderr)
        if not inside:   # systemd-managed cgroups put the instance in a scope of its own; its monitor stays in the job
            self.assertIn(pid, runner.escaped(st["units"]["cgroup_path"], st["run"]))
            wait_for(lambda: str(pid) in (self.state(d["id"]).get("escaped") or {}), 10, what="recorded as the job's")
        r = self.bus_json("release", d["id"], "--wait", "60", "--json")
        self.assertTrue(r["released"], r)
        self.assertIsNone(core.proc_ticks(pid))


if __name__ == "__main__":
    unittest.main()
