"""Runs as systemd units: the supervisor, requests, classes and bookings, admission, verified release, adoption and
hot update (design 3.3-3.8, 4.1-4.3). Needs a systemd user manager with cgroup delegation (the Rocky 9 container in
tests/container, or the GPU host); every unit is named with a test prefix and removed after."""
import json, os, secrets, shutil, signal, subprocess, sys, time, unittest

from helpers import BUS, PY, REPO, BusTest, need_systemd, wait_for
from agentbus import core, runner

SLEEP = "sleep 300"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@need_systemd
class Runs(BusTest):
    def props(self, unit, *p):
        return runner.unit_props(unit, *p)

    def request(self, rid, **kw):
        args = ["send", rid, "--kind", "request", "--gpus", "1", "--minutes", "30", "--json"]
        for k, v in kw.items():
            args += [f"--{k.replace('_', '-')}", v]
        return self.bus_json(*args)

    def craft(self, rid, **over):
        """A request message written by hand (mode, reply_to and request_id under the test's control)."""
        st = self.state(rid)
        box = f"{core.USER}-mail"
        (self.root / "inbox" / box).mkdir(exist_ok=True)
        os.chmod(self.root / "inbox" / box, 0o3775)
        mid = f"{core.USER}-20261123-000000-m{secrets.token_hex(3)}"
        m = dict(id=mid, sender=core.USER, to=rid, to_run=st["run"], kind="request", request_id=secrets.token_hex(16),
                 gpus=1, minutes=5, reply_to=box, note="", schema=2, created=int(time.time()))
        mode = over.pop("_mode", 0o644)
        m.update(over)
        p = self.root / "inbox" / rid / f"{mid}.json"
        p.write_text(json.dumps(m))
        os.chmod(p, mode)
        return m

    def replies(self, box=None):
        box = box or f"{core.USER}-mail"
        d = self.root / "inbox" / box
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def book_now(self, gpus, seconds=3600, objection="1s", start=None):
        t = int(time.time())
        r = self.bus_json("book", "--gpus", gpus, "--from", str(start or t), "--to", str((start or t) + seconds),
                          "--objection", objection, "--json")
        return r["id"]

    def wait_booking(self, bid, state, timeout=15):
        return wait_for(lambda: next((b for b in self.bus_json("cal", "--json")["bookings"] if b["id"] == bid
                                      and b["state"] == state), None), timeout, what=f"{bid} {state}")

    # --- basics

    def test_doctor_checks(self):
        d = json.loads(self.bus("doctor", "--json").stdout)
        ok = {c["check"]: c["ok"] for c in d["checks"]}
        for k in ("linger", "user manager", "cgroup2", "slice test", "board pin", "board host", "build"):
            self.assertTrue(ok[k], d)

    def test_backfill_run_entry_env_and_exit_status(self):
        out = self.tmp / "env.txt"
        p = self.bg("run", "--gpus", "2,3", "--hours", "1", "--note", "env check", "--",
                    f"echo $CUDA_VISIBLE_DEVICES $AGENT_BUS_ID > {out}; sleep 2; exit 7")
        self.assertEqual(p.wait(60), 7)
        cvd, rid = out.read_text().split()
        self.assertEqual(cvd, "2,3")
        st = self.state(rid)
        self.assertEqual((st["state"], st["class"], st["exit_status"]), ("ended", "backfill", 7))
        self.assertFalse(self.entry_file(rid).exists())
        self.assertTrue(list((self.root / "archive").glob(f"{rid}-*.json")))

    def test_entry_on_board_is_owner_0644_and_validates(self):
        d = self.start(SLEEP)
        st = os.stat(self.entry_file(d["id"]))
        self.assertEqual(st.st_mode & 0o777, 0o644)
        e = json.loads(self.entry_file(d["id"]).read_text())
        self.assertTrue(core.check_entry(e, 8))
        self.assertEqual(e["run"], d["run"])
        ls = self.bus_json("ls", "--json")
        self.assertEqual(ls["entries"][0]["occupancy"], "live")

    def test_killing_client_leaves_job_running(self):
        info = self.tmp / "i.json"
        p = self.bg("run", "--gpus", "0", "--info-file", str(info), "--", SLEEP)
        wait_for(info.exists, 20, what="info file")
        rid = json.loads(info.read_text())["id"]
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        time.sleep(1.5)
        self.assertEqual(self.props(f"{core.unit_name(rid)}-main.service", "ActiveState")["ActiveState"], "active")
        self.assertEqual(self.state(rid)["state"], "running")

    def test_mem_max_on_main_service(self):
        d = self.start(SLEEP, "0", "--mem-max", "100")
        self.assertEqual(self.props(d["main_unit"], "MemoryMax")["MemoryMax"], str(100 * 1024 * 1024))

    def test_setsid_children_stay_in_slice_and_leader_exit(self):
        d = self.start("setsid bash -c 'sleep 4' & (setsid sleep 4 &) ; exit 3")
        st = self.wait_state(d["id"], "leader-exited", 20)
        self.assertEqual(st["exit_status"], 3)
        e = json.loads(self.entry_file(d["id"]).read_text())
        self.assertEqual(e["state"], "leader-exited")
        st = self.wait_state(d["id"], "ended", 30)
        self.assertEqual(st["end_reason"], "exited rc=3")

    def test_ray_like_daemon_is_in_slice_and_stopped(self):
        pidf = self.tmp / "daemon.pid"
        d = self.start(f"python3 -c \"import os,time\nif os.fork()==0:\n os.setsid()\n if os.fork()==0:\n  "
                       f"open('{pidf}','w').write(str(os.getpid())); time.sleep(300)\n os._exit(0)\ntime.sleep(300)\"")
        wait_for(pidf.exists, 20, what="daemon pid")
        dpid = int(pidf.read_text())
        self.assertIn(core.unit_name(d["id"]) + ".slice", core.proc_cgroup(dpid))
        r = self.bus_json("release", d["id"], "--wait", "40", "--json")
        self.assertTrue(r["released"], r)
        self.assertFalse(alive(dpid))

    def test_child_moved_into_its_own_scope_is_still_the_jobs(self):
        pidf = self.tmp / "esc.pid"
        d = self.start(f"systemd-run --user --quiet --scope --unit={self.prefix}-escape -- bash -c 'echo $$ > {pidf}; "
                       f"exec sleep 300' & sleep 300")
        wait_for(pidf.exists, 20, what="the escaped child")
        pid = int(pidf.read_text())
        st = self.state(d["id"])
        self.assertNotIn(st["units"]["slice"], core.proc_cgroup(pid))
        self.assertIn(pid, runner.escaped(st["units"]["cgroup_path"], st["run"]))
        r = self.bus_json("release", d["id"], "--wait", "40", "--json")
        self.assertTrue(r["released"], r)
        self.assertFalse(alive(pid))

    # --- release

    def test_owner_release_verified_and_exit_75(self):
        info = self.tmp / "i.json"
        p = self.bg("run", "--gpus", "0", "--info-file", str(info), "--", SLEEP)
        wait_for(info.exists, 20, what="info")
        rid = json.loads(info.read_text())["id"]
        wait_for(lambda: self.entry_file(rid).exists(), 20, what="entry")
        r = self.bus_json("release", rid, "--wait", "40", "--json")
        self.assertTrue(r["released"])
        self.assertEqual(p.wait(30), runner.EXIT_RELEASED)
        st = self.state(rid)
        self.assertEqual(st["end_reason"], "released by its owner")
        self.assertEqual(runner.slice_state(st["units"]["cgroup_path"]), "gone")
        self.assertIn("released", [n["event"] for n in st["notices"]])

    def test_term_ignoring_job_killed_after_grace(self):
        d = self.start("trap '' TERM; while :; do sleep 0.2; done")
        t = time.monotonic()
        r = self.bus_json("release", d["id"], "--wait", "40", "--json")
        self.assertTrue(r["released"])
        self.assertGreaterEqual(time.monotonic() - t, 3)

    def test_populated_child_cgroup_with_empty_slice_procs_is_not_released(self):
        d = self.start(SLEEP)
        cg = self.state(d["id"])["units"]["cgroup_path"]
        self.assertEqual(open(f"/sys/fs/cgroup{cg}/cgroup.procs").read().strip(), "")
        self.assertEqual(runner.slice_state(cg), "populated")

    def test_hand_started_scope_found_and_delays_released(self):
        d = self.start(SLEEP)
        sl = self.state(d["id"])["units"]["slice"]
        h = subprocess.Popen(["systemd-run", "--user", "--quiet", "--scope", f"--slice={sl}", "--", "bash", "-c",
                              "trap '' TERM; while :; do sleep 0.2; done"], start_new_session=True)
        self.procs.append(h)
        time.sleep(1)
        self.bus("release", d["id"], check=True)
        st = wait_for(lambda: (lambda s: s if s["state"] == "stopping" and s["release"]["step"] == "term" else None)(
            self.state(d["id"])), 15, what="term step")
        self.assertEqual(runner.slice_state(st["units"]["cgroup_path"]), "populated")
        st = self.wait_state(d["id"], "ended", 40)
        self.assertEqual(st["end_reason"], "released by its owner")

    def test_nvidia_smi_failing_is_not_released(self):
        d = self.start(SLEEP)
        self.set_gpus(fail=True)
        self.bus("release", d["id"], check=True)
        st = wait_for(lambda: (lambda s: s if s.get("release") and "nvidia-smi failed" in (s["release"].get("remains")
                                                                                       or []) else None)(
            self.state(d["id"])), 30, what="remains: nvidia-smi failed")
        self.assertEqual(st["state"], "stopping")
        self.assertEqual(json.loads(self.entry_file(d["id"]).read_text())["state"], "stopping")
        self.set_gpus()
        self.wait_state(d["id"], "ended", 20)

    def test_job_process_on_gpu_delays_release(self):
        pidf = self.tmp / "gpu.pid"
        d = self.start(f"echo $$ > {pidf}; exec sleep 300", "1")
        wait_for(pidf.exists, 10, what="pid")
        pid = int(pidf.read_text())
        self.set_gpus(apps=[(1, pid)])
        self.assertTrue(runner.in_cgroup_fn(self.state(d["id"])["units"]["cgroup_path"], os.getuid())(pid))
        self.bus("release", d["id"], check=True)
        self.wait_state(d["id"], "ended", 30)   # the process dies with the slice; then the GPU check passes
        self.assertFalse(alive(pid))

    def test_unreadable_cgroup_of_own_uid_counts_as_job(self):
        other = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(other)
        env = dict(self.env, AGENT_BUS_TEST_UNREADABLE_CGROUP=str(other.pid))
        info = self.tmp / "i.json"
        r = self.bus("run", "--gpus", "0", "--info-file", str(info), "--detach", "--", SLEEP, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        rid = json.loads(info.read_text())["id"]
        self.set_gpus(apps=[(0, other.pid)])
        self.bus("release", rid, check=True)
        st = wait_for(lambda: (lambda s: s if s.get("release") and any("job processes on GPUs" in x for x in
                                                                    s["release"].get("remains") or []) else None)(
            self.state(rid)), 30, what="the unreadable pid counted as the job's")
        self.assertIn(str(other.pid), " ".join(st["release"]["remains"]))
        self.set_gpus()
        self.wait_state(rid, "ended", 20)

    # --- requests (design 3.10)

    def test_authorized_request_stops_backfill_within_a_pass(self):
        d = self.start(SLEEP)
        t = time.monotonic()
        m = self.request(d["id"])
        self.wait_state(d["id"], ("stopping", "ended"), 5)
        self.assertLess(time.monotonic() - t, 5)
        st = self.wait_state(d["id"], "ended", 40)
        self.assertTrue(st["end_reason"].startswith("request "))
        rep = wait_for(lambda: [x for x in self.replies(m["reply_to"]) if x.get("request_id") == m["request_id"]], 10,
                       what="reply")
        self.assertTrue(rep[0]["note"].startswith("released: GPUs 0 free of this job"), rep)

    def test_wrong_to_run_triggers_nothing(self):
        d = self.start(SLEEP)
        self.request(d["id"], to_run="f" * 32)
        time.sleep(2)
        st = self.state(d["id"])
        self.assertEqual(st["state"], "running")
        self.assertIn("stale", list(st["requests"].values())[0]["state"])

    def test_replayed_request_id_triggers_nothing(self):
        d = self.start(SLEEP)
        m = self.request(d["id"], to_run="f" * 32)
        wait_for(lambda: self.state(d["id"])["requests"], 10, what="first request seen")
        self.craft(d["id"], request_id=m["request_id"])
        time.sleep(2)
        self.assertEqual(self.state(d["id"])["state"], "running")

    def test_group_writable_request_is_advisory(self):
        d = self.start(SLEEP)
        self.craft(d["id"], _mode=0o664)
        wait_for(lambda: self.state(d["id"])["requests"], 10, what="request seen")
        time.sleep(1)
        st = self.state(d["id"])
        self.assertEqual(st["state"], "running")
        self.assertIn("not 0644", list(st["requests"].values())[0]["state"])

    def test_reply_to_not_senders_mailbox_is_advisory(self):
        d = self.start(SLEEP)
        self.craft(d["id"], reply_to=f"{core.USER}-nobox")
        wait_for(lambda: self.state(d["id"])["requests"], 10, what="request seen")
        self.assertEqual(self.state(d["id"])["state"], "running")

    def test_scheduled_inside_standing_window_never_stopped_then_like_backfill(self):
        bid = self.book_now("4", seconds=12)
        self.wait_booking(bid, "standing")
        d = self.start(SLEEP, "4", "--scheduled", bid)
        m = self.request(d["id"])
        rep = wait_for(lambda: [x for x in self.replies(m["reply_to"]) if x.get("request_id") == m["request_id"]], 10,
                       what="refusal")
        self.assertEqual(rep[0]["answer"], "no")
        self.assertIn("standing booking", rep[0]["reason"])
        time.sleep(2)
        self.assertEqual(self.state(d["id"])["state"], "running")   # and it did not yield to its own booking
        wait_for(lambda: time.time() > self.state(d["id"])["until"] + 1, 20, what="window end")
        self.request(d["id"])
        self.assertTrue(self.wait_state(d["id"], "ended", 40)["end_reason"].startswith("request "))

    # --- classes and bookings (design 4.1-4.3)

    def test_backfill_start_inside_standing_booking_refused(self):
        bid = self.book_now("5")
        self.wait_booking(bid, "standing")
        r = self.bus("run", "--gpus", "5,6", "--detach", "--", SLEEP)
        self.assertEqual(r.returncode, 1)
        self.assertIn(bid, r.stderr)

    def test_pending_booking_does_not_block_backfill(self):
        bid = self.book_now("5", objection="60s")
        self.assertEqual(self.wait_booking(bid, "pending")["state"], "pending")
        d = self.start(SLEEP, "5")
        self.assertEqual(self.state(d["id"])["class"], "backfill")

    def test_8_gpu_backfill_30h_allowed_and_yields_at_standing_start(self):
        d = self.start(SLEEP, "0-7", "--hours", "30")
        self.assertGreater(self.state(d["id"])["until"] - time.time(), 29 * 3600)
        bid = self.book_now("3", objection="2s")
        st = self.wait_state(d["id"], "ended", 40)
        self.assertEqual(st["end_reason"], f"yielded to booking {bid}")
        log = (self.root / "log" / f"{core.USER}.jsonl").read_text()
        self.assertIn('"yield"', log)

    def test_scheduled_on_pending_booking_is_backfill_for_stopping(self):
        a = self.book_now("2", objection="60s")
        d = self.start(SLEEP, "2", "--scheduled", a)
        self.assertEqual(self.state(d["id"])["class"], "scheduled")
        b = self.book_now("2", objection="1s")      # the owner's own other booking, standing first
        st = self.wait_state(d["id"], "ended", 40)
        self.assertEqual(st["end_reason"], f"yielded to booking {b}")

    def test_scheduled_needs_own_covering_booking(self):
        bid = self.book_now("1")
        r = self.bus("run", "--gpus", "1,2", "--scheduled", bid, "--detach", "--", SLEEP)
        self.assertIn("covers GPUs 1", r.stderr)
        r = self.bus("run", "--gpus", "1", "--scheduled", "nobody-20260101-000000-x", "--detach", "--", SLEEP)
        self.assertIn("no booking", r.stderr)

    def test_scheduled_prints_protection_end(self):
        bid = self.book_now("6")
        self.wait_booking(bid, "standing")
        info = self.tmp / "i.json"
        r = self.bus("run", "--gpus", "6", "--scheduled", bid, "--info-file", str(info), "--detach", "--", SLEEP)
        self.assertIn("protected until", r.stderr)

    def test_exclusive_handoff_waits_for_offboard_process(self):
        with open(self.root / "config", "a") as f:
            f.write("lapse_s=3\n")
        off = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(off)
        self.set_gpus(apps=[(4, off.pid)])
        bid = self.book_now("4")
        self.wait_booking(bid, "standing")
        d = self.start(SLEEP, "4", "--scheduled", bid, "--exclusive")
        st = self.wait_state(d["id"], "waiting-handoff", 10)
        wait_for(lambda: any("process" in w for w in self.state(d["id"]).get("handoff_waits") or []), 10, what="waits")
        self.assertEqual(json.loads(self.entry_file(d["id"]).read_text())["state"], "waiting-handoff")
        time.sleep(5)    # past the no-show limit: the waiting entry keeps the booking from lapsing
        self.assertEqual(self.wait_booking(bid, "standing")["state"], "standing")
        self.set_gpus()
        self.wait_state(d["id"], "running", 15)
        self.assertEqual(self.props(st["units"]["main"], "ActiveState")["ActiveState"], "active")

    def test_idle_backfill_marked(self):
        env = dict(self.env, AGENT_BUS_SAMPLE_EVERY="2", AGENT_BUS_IDLE_S="2")
        info = self.tmp / "i.json"
        self.bus("run", "--gpus", "0", "--info-file", str(info), "--detach", "--", SLEEP, env=env, check=True)
        rid = json.loads(info.read_text())["id"]
        wait_for(lambda: json.loads(self.entry_file(rid).read_text()).get("idle") if self.entry_file(rid).exists()
                 else None, 20, what="idle mark")

    # --- admission (design 3.5)

    def exec_bg(self, rid, run, *cmd, pause=None):
        env = dict(self.env, **({"AGENT_BUS_TEST_EXEC_PAUSE": pause} if pause else {}))
        return self.bg("exec", rid, "--run", run, "--", *cmd, env=env)

    def intents(self, rid):
        return runner.admit_read(rid)["inflight"]

    def test_exec_registered_and_stopped_with_the_slice(self):
        d = self.start(SLEEP)
        p = self.exec_bg(d["id"], d["run"], "sleep", "300")
        wait_for(lambda: [x for x in self.intents(d["id"]) if x["state"] == "registered"], 15, what="registered")
        self.bus_json("release", d["id"], "--wait", "40", "--json")
        self.assertIsNotNone(p.wait(20))

    def test_exec_after_close_and_stale_run_refused(self):
        d = self.start(SLEEP)
        r = self.bus("exec", d["id"], "--run", "0" * 32, "--", "true")
        self.assertIn("stale exec", r.stderr)
        self.bus_json("release", d["id"], "--wait", "40", "--json")
        r = self.bus("exec", d["id"], "--run", d["run"], "--", "true")
        self.assertIn("closed", r.stderr)

    def test_paused_exec_holds_stopping_then_registers_and_is_stopped(self):
        d = self.start(SLEEP)
        p = self.exec_bg(d["id"], d["run"], "sleep", "300", pause="admitted")
        wait_for(lambda: self.intents(d["id"]), 15, what="admitted intent")
        self.bus("release", d["id"], check=True)
        st = wait_for(lambda: (lambda s: s if s.get("release") and any("outstanding" in x for x in
                                                                    s["release"].get("remains") or []) else None)(
            self.state(d["id"])), 15, what="exec outstanding")
        time.sleep(3)    # no timeout shortcut
        self.assertEqual(self.state(d["id"])["state"], "stopping")
        os.killpg(p.pid, signal.SIGCONT)
        self.wait_state(d["id"], "ended", 40)
        self.assertEqual([x["state"] for x in self.intents(d["id"])], ["registered"])
        self.assertIsNotNone(p.wait(20))

    def test_paused_exec_killed_is_cancelled(self):
        d = self.start(SLEEP)
        p = self.exec_bg(d["id"], d["run"], "sleep", "300", pause="admitted")
        wait_for(lambda: self.intents(d["id"]), 15, what="admitted intent")
        self.bus("release", d["id"], check=True)
        time.sleep(2)
        self.assertEqual(self.state(d["id"])["state"], "stopping")
        os.killpg(p.pid, signal.SIGKILL)
        self.wait_state(d["id"], "ended", 40)
        self.assertEqual([x["state"] for x in self.intents(d["id"])], ["cancelled"])

    def test_exec_killed_while_launching_is_cancelled(self):
        d = self.start(SLEEP)
        p = self.exec_bg(d["id"], d["run"], "sleep", "300", pause="launching")
        wait_for(lambda: [x for x in self.intents(d["id"]) if x["state"] == "launching"], 15, what="launching")
        self.bus("release", d["id"], check=True)
        time.sleep(2)
        self.assertEqual(self.state(d["id"])["state"], "stopping")
        os.killpg(p.pid, signal.SIGKILL)
        self.wait_state(d["id"], "ended", 40)
        self.assertEqual([x["state"] for x in self.intents(d["id"])], ["cancelled"])

    def test_paused_exec_holds_leader_exit(self):
        d = self.start("sleep 3")
        p = self.exec_bg(d["id"], d["run"], "sleep", "300", pause="admitted")
        wait_for(lambda: self.intents(d["id"]), 15, what="admitted intent")
        st = self.wait_state(d["id"], "leader-exited", 20)
        self.assertTrue(any("outstanding" in x for x in st.get("leader_remains") or []), st.get("leader_remains"))
        os.killpg(p.pid, signal.SIGKILL)
        self.wait_state(d["id"], "ended", 30)

    # --- supervisor robustness

    def test_hook_that_never_returns_is_killed_and_loop_continues(self):
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/bash\necho $$ >> {self.tmp}/hook.pids\nexec sleep 1000\n")
        hook.chmod(0o755)
        d = self.start(SLEEP, "0", "--on-message", str(hook))
        self.bus("release", d["id"], check=True)
        wait_for(lambda: (self.tmp / "hook.pids").exists(), 10, what="hook ran")
        st = self.wait_state(d["id"], "ended", 40)
        time.sleep(3)
        pids = [int(x) for x in (self.tmp / "hook.pids").read_text().split()]
        self.assertFalse(any(alive(p) for p in pids), "hooks killed at the timeout")
        self.assertEqual(st["end_reason"], "released by its owner")

    def test_stalled_loop_shows_occupancy_unknown(self):
        stall = self.tmp / "stall"
        env = dict(self.env, AGENT_BUS_TEST_STALL=str(stall), AGENT_BUS_HB_STALE_S="3")
        info = self.tmp / "i.json"
        self.bus("run", "--gpus", "0", "--info-file", str(info), "--detach", "--", SLEEP, env=env, check=True)
        rid = json.loads(info.read_text())["id"]
        wait_for(lambda: self.entry_file(rid).exists(), 20, what="entry")
        stall.touch()
        wait_for(lambda: self.bus_json("ls", "--json", env=env)["entries"][0]["occupancy"] == "unknown", 15,
                 what="occupancy unknown")
        stall.unlink()
        wait_for(lambda: self.bus_json("ls", "--json", env=env)["entries"][0]["occupancy"] == "live", 15,
                 what="live again")

    def adopt_timer(self):
        name = f"{self.prefix}-adopt"
        env = dict(self.env, AGENT_BUS_TIMER=name)
        r = subprocess.run(["bash", str(REPO / "bin" / "install.sh"), "--board", str(self.root)], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ud = os.path.expanduser("~/.config/systemd/user")
        self.addCleanup(lambda: [os.unlink(os.path.join(ud, f"{name}.{x}")) for x in ("timer", "service")
                                 if os.path.exists(os.path.join(ud, f"{name}.{x}"))])
        self.addCleanup(subprocess.run, ["systemctl", "--user", "stop", f"{name}.timer"], capture_output=True)
        subprocess.run(["systemctl", "--user", "start", f"{name}.timer"], check=True)
        return name

    def test_sigkill_supervisor_adopted_by_timer_heartbeat_and_requests_kept(self):
        d = self.start(SLEEP)
        self.request(d["id"], to_run="f" * 32)
        wait_for(lambda: self.state(d["id"])["requests"], 10, what="request recorded")
        first = dict(self.state(d["id"])["requests"])
        self.adopt_timer()
        old = self.state(d["id"])["supervisor"]["pid"]
        os.kill(old, signal.SIGKILL)
        t = time.time()
        new = wait_for(lambda: (lambda s: s["supervisor"]["pid"] if s["supervisor"]["pid"] != old else None)(
            self.state(d["id"])), 60, step=0.5, what="adopted")
        gap = time.time() - t
        self.assertLess(gap, 45)
        hb = self.root / "agents" / ".heartbeat" / d["id"]
        wait_for(lambda: time.time() - hb.stat().st_mtime < 2, 10, what="fresh heartbeat")
        self.assertEqual(self.state(d["id"])["requests"], first)
        self.assertTrue(self.state(d["id"])["adoptions"])
        print(f"\n[adoption] supervisor {old} SIGKILLed, {new} adopted after {gap:.1f} s", file=sys.stderr)

    def test_two_adopters_one_holds_the_lock(self):
        d = self.start(SLEEP)
        old = self.state(d["id"])["supervisor"]["pid"]
        os.kill(old, signal.SIGKILL)
        time.sleep(0.5)
        a = self.bg("supervise", d["id"], "--adopt")
        b = self.bg("supervise", d["id"], "--adopt")
        time.sleep(3)
        codes = sorted([p.poll() for p in (a, b)], key=lambda x: (x is not None, x))
        self.assertEqual(codes[0], None)
        self.assertEqual(codes[1], 3)

    def other_build(self, marker):
        src = self.tmp / f"src-{marker}"
        shutil.copytree(REPO / "bin", src / "bin")
        shutil.copytree(REPO / "lib", src / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        with open(src / "lib" / "agentbus" / "__init__.py", "a") as f:
            f.write(f"# build {marker}\n")
        return src

    def test_upgrade_mid_run_execs_and_carries_state_then_rolls_back(self):
        d = self.start(SLEEP)
        before = self.state(d["id"])
        r = self.bus_json("upgrade", "--build", str(self.other_build("two")), "--json")
        st = wait_for(lambda: (lambda s: s if s["supervisor"]["code"] == r["installed"] else None)(self.state(d["id"])),
                      20, what="supervisor on the new build")
        gap = st["adoptions"][-1]["t"] - st["exec_at"]
        self.assertLess(gap, 5)
        self.assertEqual(st["supervisor"]["pid"], before["supervisor"]["pid"])  # execv keeps the pid
        self.assertEqual((st["run"], st["started"]), (before["run"], before["started"]))
        self.assertEqual(self.props(d["main_unit"], "ActiveState")["ActiveState"], "active")
        back = self.bus_json("upgrade", "--build", str(self.home / "builds" / r["previous"]), "--json")
        wait_for(lambda: self.state(d["id"])["supervisor"]["code"] == back["installed"], 20, what="rolled back")
        print(f"\n[upgrade] exec gap {gap:.3f} s", file=sys.stderr)

    def test_failed_exec_supervisor_exits_and_is_adopted_after_rollback(self):
        d = self.start(SLEEP)
        prev = runner.installed_build()
        self.assertEqual(self.state(d["id"])["supervisor"]["code"], prev)
        broken = self.other_build("broken")
        (broken / "lib" / "agentbus" / "cli.py").write_text("raise SystemExit(5)\n")
        self.bus_json("upgrade", "--build", str(broken), "--json")
        lock = self.home / "state" / "runs" / f"{d['id']}.lock"
        wait_for(lambda: runner.lock_free(str(lock)), 20, what="the supervisor gone")
        # with the installed build broken, the rollback runs the previous build's own entry point
        r0 = subprocess.run([PY, "-IB", str(self.home / "builds" / prev / "bin" / "bus"), "upgrade", "--build",
                             str(self.home / "builds" / prev), "--json"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r0.returncode, 0, r0.stderr)
        back = json.loads(r0.stdout)
        self.assertEqual(back["installed"], prev, back)
        r = self.bus_json("adopt", "--all")
        self.assertTrue(r and r[0]["started"], r)
        try:
            wait_for(lambda: not runner.lock_free(str(lock)), 20, what="adopted")
        except AssertionError:
            u = r[0]["unit"]
            print(subprocess.run(["systemctl", "--user", "status", u, "--no-pager", "-n", "30"], capture_output=True,
                                 text=True).stdout, json.dumps(self.state(d["id"]).get("supervisor")),
                  json.dumps(self.state(d["id"]).get("pass_errors")), file=sys.stderr)
            raise
        self.assertEqual(self.state(d["id"])["state"], "running")

    def test_newer_state_version_refused_and_owner_told(self):
        d = self.start(SLEEP)
        pid = self.state(d["id"])["supervisor"]["pid"]
        os.kill(pid, signal.SIGKILL)
        p = self.home / "state" / "runs" / f"{d['id']}.json"
        s = json.loads(p.read_text())
        s["state_version"] = 99
        p.write_text(json.dumps(s))
        r = self.bus("supervise", d["id"], "--adopt")
        self.assertEqual(r.returncode, 1)
        self.assertIn("newer", r.stderr)
        self.assertIn('"refused"', (self.home / "state" / "notices.jsonl").read_text())

    def test_migration_from_version_1_to_test_version_2(self):
        d = self.start(SLEEP)
        os.kill(self.state(d["id"])["supervisor"]["pid"], signal.SIGKILL)
        time.sleep(0.3)
        env = dict(self.env, AGENT_BUS_TEST_STATE_VERSION="2")
        self.bg("supervise", d["id"], "--adopt", env=env)
        st = wait_for(lambda: (lambda s: s if s["state_version"] == 2 else None)(self.state(d["id"])), 15,
                      what="migrated")
        self.assertEqual(st["migrated_from"], 1)

    def test_adopter_finishes_a_release_the_dead_supervisor_did_not_record(self):
        d = self.start(SLEEP)
        st = self.state(d["id"])
        os.kill(st["supervisor"]["pid"], signal.SIGKILL)
        subprocess.run(["systemctl", "--user", "kill", "--signal=KILL", st["units"]["slice"]])
        wait_for(lambda: runner.slice_state(st["units"]["cgroup_path"]) != "populated", 10, what="slice empty")
        self.bus_json("adopt", "--all")
        st = self.wait_state(d["id"], "ended", 30)
        self.assertFalse(self.entry_file(d["id"]).exists())
        self.assertTrue(list((self.root / "archive").glob(f"{d['id']}-*.json")))

    def test_run_whose_units_never_started_recorded_ended(self):
        rid = f"{core.USER}-20261123-000000-ghost"
        un = core.unit_name(rid)
        s = dict(state_version=1, id=rid, run="a" * 32, host=core.HOST, user=core.USER, state="starting",
                 entry_state="running", mode="systemd", handoff=False, gpus=[0], started=1, until=2, kind="other",
                 units=dict(slice=f"{un}.slice", main=f"{un}-main.service", sup=f"{un}-sup.service", cgroup_path=None),
                 board=dict(path=str(self.root), inode=0, fsid=0), requests={}, notices=[])
        s["class"] = "backfill"
        core.write_private(runner.state_path(rid), s)
        self.bus_json("adopt", "--all")
        st = wait_for(lambda: (lambda x: x if x["state"] == "ended" else None)(self.state(rid)), 15, what="ended")
        self.assertEqual(st["end_reason"], "its units never started")

    def test_board_replaced_while_running(self):
        d = self.start(SLEEP)
        os.rename(self.root, self.tmp / "aside")
        r = subprocess.run(["bash", str(REPO / "setup-board.sh"), str(self.root), self.host(), "--blocks", "1"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        st = wait_for(lambda: (lambda s: s if s.get("identity_alarm") else None)(self.state(d["id"])), 10,
                      what="identity alarm")
        time.sleep(1.5)
        self.assertEqual(os.listdir(self.root / "agents"), [".heartbeat"])
        self.assertEqual(os.listdir(self.root / "agents" / ".heartbeat"), [])
        self.assertEqual(os.listdir(self.root / "log"), [])
        self.assertEqual(self.props(d["main_unit"], "ActiveState")["ActiveState"], "active")
        self.assertIn("board-identity-changed", [n["event"] for n in st["notices"]])
        self.craft_on(self.root, d["id"], st["run"])      # a request on the replacing board triggers nothing
        time.sleep(1.5)
        self.assertEqual(self.state(d["id"])["state"], "running")

    def craft_on(self, root, rid, run):
        (root / "inbox" / rid).mkdir(exist_ok=True)
        mid = f"{core.USER}-20261123-000000-mx"
        (root / "inbox" / f"{core.USER}-mail").mkdir(exist_ok=True)
        m = dict(id=mid, sender=core.USER, to=rid, to_run=run, kind="request", request_id=secrets.token_hex(16),
                 gpus=1, minutes=5, reply_to=f"{core.USER}-mail", note="", schema=2, created=int(time.time()))
        (root / "inbox" / rid / f"{mid}.json").write_text(json.dumps(m))

    # --- session tooling

    def test_stop_hook_blocks_until_watched(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID="sess-1")
        info = self.tmp / "i.json"
        self.bus("run", "--gpus", "0", "--info-file", str(info), "--detach", "--", SLEEP, env=env, check=True)
        rid = json.loads(info.read_text())["id"]
        r = self.bus("stop-hook", inp=json.dumps({"session_id": "sess-1"}))
        self.assertEqual(json.loads(r.stdout)["decision"], "block")
        self.assertEqual(self.bus("stop-hook", inp=json.dumps({"session_id": "other"})).stdout, "")
        w = self.bg("watch", rid)
        time.sleep(1.5)
        self.assertEqual(self.bus("stop-hook", inp=json.dumps({"session_id": "sess-1"})).stdout, "")
        w.kill()

    def test_watch_prints_release_notices_and_writes_nothing_on_board(self):
        d = self.start(SLEEP)
        before = sorted(str(p) for p in self.root.rglob("*"))
        w = self.bg("watch", d["id"], "--interval", "0.3")
        time.sleep(1)
        after = sorted(str(p) for p in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.bus("release", d["id"], check=True)
        out, _ = w.communicate(timeout=60)
        self.assertIn("NOTICE", out)
        self.assertIn("released", out)
        self.assertIn(f"WATCH {d['id']} ended", out)

    def test_gc_lists_orphan_and_archives_own_stale_entry(self):
        d = self.start(SLEEP)
        os.kill(self.state(d["id"])["supervisor"]["pid"], signal.SIGKILL)
        stale = dict(id=f"{core.USER}-20261123-000000-stale", user=core.USER, host="h", gpus=[0], booking=None,
                     run="a" * 32, started=1, until=2, kind="other", exclusive=False, note="", state="running",
                     schema=2)
        stale["class"] = "backfill"
        (self.root / "agents" / f"{stale['id']}.json").write_text(json.dumps(stale))
        time.sleep(0.5)
        r = self.bus_json("gc", "--mine", "--json")
        self.assertEqual([o["id"] for o in r["orphans"]], [d["id"]])
        self.assertEqual(r["archived"], [stale["id"]])
        self.assertTrue(self.entry_file(d["id"]).exists())

    def test_reply_and_mailbox(self):
        box = self.bus("mailbox", "talk", check=True).stdout.strip()
        self.assertEqual(box, f"{core.USER}-talk")
        self.assertEqual(os.stat(self.root / "inbox" / box).st_mode & 0o7777, 0o3775)
        d = self.start(SLEEP)
        m = self.request(d["id"], to_run="f" * 32, reply_to=box)
        msg = next(p.stem for p in (self.root / "inbox" / d["id"]).glob("*.json"))
        self.bus("reply", msg, "--answer", "no", check=False)
        self.assertIn("reason", self.bus("reply", msg, "--answer", "no").stderr)
        self.bus("reply", msg, "--answer", "no", "--reason", "still training", check=True)
        self.assertTrue(any(r.get("answer") == "no" and r.get("request_id") == m["request_id"]
                            for r in self.replies(box)))


@need_systemd
class Fallback(BusTest):
    def test_fallback_release_waits_for_escaped_descendant_until_reconciled(self):
        env = dict(self.env, AGENT_BUS_SYSTEMD="off")
        pidf = self.tmp / "esc.pid"
        info = self.tmp / "i.json"
        p = self.bg("run", "--gpus", "0", "--info-file", str(info), "--",
                    f"setsid sleep 300 & echo $! > {pidf}; sleep 300", env=env)
        wait_for(pidf.exists, 20, what="escaped pid")
        esc = int(pidf.read_text())
        self.procs.append(subprocess.Popen(["true"]))
        rid = json.loads(info.read_text())["id"]
        self.assertEqual(self.state(rid)["mode"], "fallback")
        self.set_gpus(apps=[(0, esc)])
        self.bus("release", rid, env=env, check=True)
        st = wait_for(lambda: (lambda s: s if s.get("release") and s["release"]["step"] == "verify" and
                               any("job processes" in x for x in s["release"].get("remains") or []) else None)(
            self.state(rid)), 30, what="waiting for reconciliation")
        r = json.loads(self.bus("reconcile", rid, env=env).stdout)
        self.assertEqual(r["candidates"], [esc])
        self.bus("reconcile", rid, "--confirm", str(esc), env=env, check=True)
        self.wait_state(rid, "ended", 20)
        self.assertEqual(p.wait(20), runner.EXIT_RELEASED)
        os.kill(esc, signal.SIGKILL)

    def test_fallback_has_no_exec(self):
        env = dict(self.env, AGENT_BUS_SYSTEMD="off")
        info = self.tmp / "i.json"
        self.bg("run", "--gpus", "0", "--info-file", str(info), "--", "sleep 30", env=env)
        wait_for(info.exists, 20, what="info")
        d = json.loads(info.read_text())
        r = self.bus("exec", d["id"], "--run", d["run"], "--", "true", env=env)
        self.assertIn("fallback", r.stderr)
        self.bus("release", d["id"], "--wait", "30", env=env)


if __name__ == "__main__":
    unittest.main()
