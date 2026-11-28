"""Check public release tags and activate verified builds through the installed bus symlink."""
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from time import time as wall_time

from . import core, runner
from .core import Refused

REPO = "https://github.com/andylizf/agent-bus.git"
SOAK_S = 24 * 60 * 60
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
OID = re.compile(r"[0-9a-f]{40}")
PASS_ALLOWANCE_S = 6 * 60  # Up to two 60 s GPU views plus systemd, admission and hook waits in a pass.
TAKEOVER_S = 2 * PASS_ALLOWANCE_S + 60
SERVICE_TIMEOUT_S = 45 * 60  # Allows a candidate trial and a full rollback verification.
RECOVERY_TIMEOUT_S = TAKEOVER_S + 2 * 60
PENDING_STALE_S = TAKEOVER_S + 5 * 60  # Includes the probes before and after the switch.
RETRY_DELAYS_S = (15 * 60, 60 * 60)
WITHDRAWALS = "auto-upgrade-withdrawn"


class TagChanged(Refused):
    def __init__(self, tag, expected, actual):
        self.tag, self.actual = tag, actual
        super().__init__(f"{tag} changed during fetch: expected {expected[0]} ({expected[1]}), "
                         f"got {actual[0]} ({actual[1]})")


class TagDeleted(Refused):
    def __init__(self, tag):
        self.tag = tag
        super().__init__(f"{tag} disappeared during fetch")


def version(tag):
    match = TAG.fullmatch(tag)
    return tuple(map(int, match.groups())) if match else None


def git(*args, binary=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return run("git", "-c", "credential.helper=", "-c", "http.followRedirects=false", *args, env=env, binary=binary)


def run(*args, env=None, binary=False, timeout=90):
    try:
        proc = subprocess.run(args, env=env, cwd="/", capture_output=True, text=not binary, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"{args[0]}: {e}") from e
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise Refused(f"{args[0]} exited {proc.returncode}: {detail or 'no output'}")
    return proc.stdout if binary else proc.stdout.strip()


def release_refs():
    refs, peeled = {}, {}
    for line in git("ls-remote", "--tags", REPO).splitlines():
        oid, sep, ref = line.partition("\trefs/tags/")
        tag = ref.removesuffix("^{}")
        if not sep or not version(tag):
            continue
        if not OID.fullmatch(oid):
            raise Refused(f"invalid object ID for {ref}")
        (refs if tag == ref else peeled)[tag] = oid
    return {tag: (oid, peeled.get(tag, oid)) for tag, oid in refs.items()}


def fetch(src, tag, oid, commit):
    ref = f"refs/tags/{tag}"
    git("init", "--quiet", "--template=", src)
    try:
        git("-C", src, "-c", "fetch.fsckObjects=true", "fetch", "--quiet", "--depth=1", "--no-tags", REPO, ref)
    except Refused as e:
        if str(e).partition("\n")[0].endswith(f"fatal: couldn't find remote ref {ref}"):
            raise TagDeleted(tag) from e
        raise
    actual_oid = git("-C", src, "rev-parse", "FETCH_HEAD")
    try:
        actual_commit = git("-C", src, "rev-parse", "FETCH_HEAD^{commit}")
    except Refused:
        if actual_oid != oid:
            raise TagChanged(tag, (oid, commit), (actual_oid, None)) from None
        raise
    actual = (actual_oid, actual_commit)
    if actual != (oid, commit):
        raise TagChanged(tag, (oid, commit), actual)


def release_blobs(src, commit):
    raw = git("-C", src, "ls-tree", "-rz", commit, "--", "bin", "lib", WITHDRAWALS, binary=True)
    for record in raw.split(b"\0")[:-1]:
        header, name = record.split(b"\t", 1)
        mode, kind, oid = header.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise Refused(f"release path is not a regular file: {os.fsdecode(name)}")
        if any(part in (b"", b".", b"..") for part in name.split(b"/")):
            raise Refused(f"invalid release path: {name!r}")
        if b"/" not in name or not name.startswith((b"bin/", b"lib/")):
            if name != WITHDRAWALS.encode():
                raise Refused(f"unexpected release path: {name!r}")
        yield os.fsdecode(name), int(mode[-3:], 8), git("-C", src, "cat-file", "blob", oid.decode(), binary=True)


def stage_release(src, tag, commit):
    os.makedirs(core.BUILDS(), exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix=f"release-{commit}-{tag}-", dir=core.BUILDS()))
    withdrawn = []
    try:
        for name, mode, data in release_blobs(src, commit):
            if name == WITHDRAWALS:
                withdrawn = data.decode("ascii", errors="replace").split()
                if any(not version(old) or version(old) >= version(tag) for old in withdrawn):
                    raise Refused(f"{WITHDRAWALS} must list only older vX.Y.Z tags")
                continue
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(mode)
        bus = build / "bin" / "bus"
        if not os.access(bus, os.X_OK) or not bus.is_file() or not (build / "lib" / "agentbus").is_dir():
            raise Refused("verified release needs an executable bin/bus and lib/agentbus")
        core.write_private(build / runner.BUILD_RELEASE, dict(tag=tag, base=tag, commit=commit))
        return str(build), withdrawn
    except (OSError, Refused):
        shutil.rmtree(build, ignore_errors=True)
        raise


def source_release(src, dirty):
    """Find the release or release ancestor of a manually installed checkout."""
    try:
        if git("-C", src, "rev-parse", "--show-toplevel") != src:
            return {}
        exact = git("-C", src, "tag", "--points-at", "HEAD").splitlines() if not dirty else []
        tags = [tag for tag in exact if version(tag)]
        if tags:
            tag = max(tags, key=version)
            return dict(tag=tag, base=tag)
        merged = git("-C", src, "tag", "--merged", "HEAD").splitlines()
        tags = [tag for tag in merged if version(tag)]
        return dict(base=max(tags, key=version)) if tags else {}
    except Refused:
        return {}


def settings(update=None):
    path = os.path.join(core.CONFIG(), "auto-upgrade.conf")
    conf = core.load_conf(path)
    if update:
        conf.update(update)
        core.save_conf(conf, path)
    return conf


def unit_name():
    prefix = core.PREFIX()
    return f"{core.component(prefix)}-auto-upgrade" if prefix != "agentbus" else "bus-auto-upgrade"


def unit_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config", "systemd", "user")


def linger_status():
    try:
        return subprocess.run(["loginctl", "show-user", core.USER, "-p", "Linger", "--value"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return repr(e)


def service_entry():
    return os.path.join(core.STATE(), "service-entry.py")


def link_to_build(link, build):
    os.makedirs(os.path.dirname(link), exist_ok=True)
    tmp = link + f".new{secrets.token_hex(3)}"
    os.symlink(os.path.join(build, "bin", "bus"), tmp)
    try:
        os.replace(tmp, link)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def bootstrap():
    """An older upgrader copies only bin/ and lib/. Initialize that build on its first invocation."""
    build = runner.build_root()
    if (os.path.dirname(build) != os.path.realpath(core.BUILDS()) or build != runner.build_root(core.BIN())
            or not core.load_conf().get("host")):
        return
    metadata = Path(build, runner.BUILD_RELEASE)
    if metadata.is_file():
        current = runner.build_release(build)
        if "legacy_build" not in current or os.path.exists(service_entry()) or settings().get("enabled") == "off":
            return
    upgrade_lock = lock()
    if not upgrade_lock.acquire():
        return
    try:
        if not metadata.is_file():
            bid = runner.build_id(build)
            core.write_private(metadata, {"legacy_build": bid})
        sync_timer()
    finally:
        upgrade_lock.release()


def install_timer():
    host = core.load_conf().get("host")
    if not host:
        raise Refused("no board host in board.conf")
    local = socket.gethostname()
    domain = local.partition(".")[2]
    fqdn = f"{host}.{domain}" if domain and "." not in host else host
    core.component(fqdn, "board host")
    units = unit_dir()
    units.mkdir(parents=True, exist_ok=True)
    entry = service_entry()
    path = os.path.join(runner.build_root(), "bin", "service-entry.py")
    core.private_dir()
    tmp = entry + f".new{secrets.token_hex(3)}"
    try:
        shutil.copyfile(path, tmp)
        os.replace(tmp, entry)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    name = unit_name()
    env = "\n".join(f"Environment={json.dumps(key + '=' + os.environ[key].replace('%', '%%'))}"
                    for key in ("AGENT_BUS_BIN", "AGENT_BUS_CONFIG", "AGENT_BUS_STATE", "AGENT_BUS_BUILDS",
                                "AGENT_BUS_UNIT_PREFIX") if key in os.environ)
    executable = " ".join(json.dumps(p.replace("%", "%%").replace("$", "$$"))
                          for p in (sys.executable, "-IB", entry))
    unit = f"[Unit]\nDescription=agent-bus automatic upgrade\nConditionHost={fqdn}\n"
    (units / f"{name}.service").write_text(
        f"{unit}\n[Service]\nType=oneshot\n{env}\nExecStart={executable} _auto-upgrade-timer\n"
        f"ExecStopPost={executable} _auto-upgrade-recover\nTimeoutStartSec={SERVICE_TIMEOUT_S}\n"
        f"TimeoutStopSec={RECOVERY_TIMEOUT_S}\n")
    (units / f"{name}.timer").write_text(
        f"{unit}\n[Timer]\nOnActiveSec=15min\nOnUnitActiveSec=1d\n\n[Install]\nWantedBy=timers.target\n")
    adopt = core.component(os.environ.get("AGENT_BUS_TIMER") or "bus-adopt", "adopt unit")
    if (units / f"{adopt}.service").is_file():
        drop_in = units / f"{adopt}.service.d"
        drop_in.mkdir(exist_ok=True)
        (drop_in / "auto-upgrade.conf").write_text(
            f"[Service]\nExecStart=\nExecStart={executable} adopt --all\nTimeoutStartSec={RECOVERY_TIMEOUT_S}\n")


def lock(shared=False):
    return runner.Locked(os.path.join(core.private_dir(), "upgrade.lock"), shared=shared)


def configure(action, allow_major=False):
    with lock():
        if action != "setup":
            update = {"enabled": action}
            if action == "on":
                update["allow_major"] = "yes" if allow_major else "no"
            settings(update)
        return sync_timer()


def sync_timer():
    enabled = settings().get("enabled") != "off"
    timer = unit_name() + ".timer"
    if enabled:
        install_timer()
    elif not (unit_dir() / timer).exists():
        return "off"
    if not runner.user_manager_ok() or (enabled and linger_status() != "yes"):
        wanted = unit_dir() / "timers.target.wants" / timer
        if enabled:
            wanted.parent.mkdir(exist_ok=True)
            try:
                wanted.symlink_to(unit_dir() / timer)
            except FileExistsError:
                pass
        elif wanted.is_symlink() and wanted.resolve() == (unit_dir() / timer).resolve():
            wanted.unlink()
        return "on (timer unavailable: run loginctl enable-linger and bus auto-upgrade on)" if enabled else "off"
    args = ["daemon-reload"] if enabled else ["disable", "--now", timer]
    rc, out, err = runner.sysctl(*args)
    if not rc and enabled:
        rc, out, err = runner.sysctl("enable", "--now", timer)
    if rc:
        raise Refused(f"auto-upgrade timer: {(err or out).strip() or 'systemctl failed'}")
    return "on" if enabled else "off"


def state_file():
    return os.path.join(core.STATE(), "auto-upgrade.json")


def read_state():
    return core.read_private(state_file()) or {}


def request_check():
    if settings().get("enabled") == "off":
        return "off", 0
    if not runner.user_manager_ok() or linger_status() != "yes":
        raise Refused('timer unavailable; run loginctl enable-linger "$USER", then bus auto-upgrade on')
    previous = read_state()
    rc, out, err = runner.sysctl("start", unit_name() + ".service", timeout=SERVICE_TIMEOUT_S + 60)
    state = read_state()
    if (state.get("last_check"), state.get("last_result")) != (previous.get("last_check"), previous.get("last_result")) \
            and state.get("last_result"):
        return state["last_result"], int(rc != 0)
    if rc:
        raise Refused(f"upgrade service: {(err or out).strip() or 'systemctl start failed'}")
    return "service finished without a new check", 0


def record_failure(state, tag, commit, build, reason):
    retries = state.setdefault("retries", {})
    attempts = retries.get(commit, {}).get("attempts", 0) + 1
    failure = dict(build=build, reason=reason, attempts=attempts)
    if attempts > len(RETRY_DELAYS_S):
        retries.pop(commit, None)
        state.setdefault("quarantine", {})[commit] = failure
        detail = "quarantined after 3 failures"
    else:
        delay = RETRY_DELAYS_S[attempts - 1]
        retries[commit] = dict(failure, retry_at=wall_time() + delay)
        detail = f"attempt {attempts}/3, retry in {delay // 60} min"
    return f"failed: {tag} {detail}: {reason}"


def rollback(state, flip, reason):
    pending = state["pending"]
    if "failure" not in pending:
        pending["failure"] = reason
        pending["failure_result"] = record_failure(state, pending["to"], pending["commit"], pending["build"], reason)
    result = pending.get("failure_result") or state["last_result"]
    state["last_result"] = result
    core.write_private(state_file(), state)
    try:
        previous = pending["previous"]
        current = runner.build_root(core.BIN())
        if current != previous and os.path.basename(current) == pending["build"]:
            flip(previous)
        elif current != previous:
            # The operator used another entry point while the trial was pending.
            state["last_result"] = f"{result}; kept operator-selected build {os.path.basename(current)}"
            del state["pending"]
            core.write_private(state_file(), state)
            return state["last_result"]
        restored_at_ns = time.monotonic_ns()
        active = pending.get("runs")
        if active is None:
            active = {r["id"]: r["run"] for r in live_runs() if r["mode"] == "systemd"}
        if active:
            runner.adopt_runs(only=set(active))
            wait_for_takeover(os.path.basename(previous), active, after_ns=restored_at_ns)
            state["last_result"] = f"{result}; previous build supervising all live runs"
        del state["pending"]
    except (OSError, Refused) as e:
        state["last_result"] = f"{result}; rollback recovery failed: {e}"
    core.write_private(state_file(), state)
    return state["last_result"]


def settle(flip, stale_only=False):
    state = read_state()
    pending = state.get("pending")
    if not pending:
        return None
    if stale_only:
        host = core.load_conf().get("host")
        if host not in (core.HOST, socket.gethostname()) or wall_time() - pending.get("time", 0) < PENDING_STALE_S:
            return None
    return rollback(state, flip, "takeover was interrupted")


def doctor(user_manager, linger):
    enabled = settings().get("enabled") != "off"
    installed = runner.installed_build()
    current = runner.build_release(runner.build_root(core.BIN()))
    state = read_state()
    if not user_manager or not linger:
        timer = 'unavailable (fallback: run loginctl enable-linger "$USER", then bus auto-upgrade on)'
    elif not enabled:
        timer = "off"
    else:
        _, out, _ = runner.sysctl("is-active", unit_name() + ".timer")
        timer = out.strip() or "unavailable"
    pending = state.get("pending")
    result = state.get("last_result") or (f"verifying takeover of {pending['to']}" if pending else None)
    return dict(enabled=enabled, timer=timer, current_tag=current.get("tag"), current_build=installed,
                base=current.get("base"), quarantine=state.get("quarantine", {}), retries=state.get("retries", {}),
                last_check=state.get("last_check"), last_result=result)


def installed_release(refs):
    build = Path(runner.build_root(core.BIN()))
    current = runner.build_release(str(build))
    legacy = current.get("legacy_build", "")
    if current.get("base") or not re.fullmatch(r"[0-9a-f]{12,40}", legacy):
        return current
    matches = [(tag, oid, commit) for tag, (oid, commit) in refs.items() if commit.startswith(legacy)]
    if not matches or len({commit for _, _, commit in matches}) != 1:
        return current
    tag, oid, commit = max(matches, key=lambda ref: version(ref[0]))
    with tempfile.TemporaryDirectory(prefix="identify-", dir=core.STATE()) as src:
        fetch(src, tag, oid, commit)
        expected = {name: (mode & stat.S_IXUSR, data) for name, mode, data in release_blobs(src, commit)
                    if name != WITHDRAWALS}
        files = [build / "bin", build / "lib"]
        files += [p for part in ("bin", "lib") for p in (build / part).rglob("*")]
        if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in files):
            raise Refused("legacy build contains a non-regular path")
        actual = {p.relative_to(build).as_posix(): (p.stat().st_mode & stat.S_IXUSR, p.read_bytes())
                  for p in files if p.is_file()}
        if actual != expected:
            raise Refused("legacy build files differ from the matching release tag")
    current = dict(tag=tag, base=tag, commit=commit)
    core.write_private(build / runner.BUILD_RELEASE, current)
    return current


def probe(build, runs):
    data = run(sys.executable, "-IB", os.path.join(build, "bin", "bus"), "_upgrade-probe", *runs, timeout=30)
    try:
        result = json.loads(data)
        if result["build"] == os.path.basename(build) and type(result["state_version"]) is int:
            return result["state_version"]
    except (TypeError, KeyError, ValueError):
        pass
    raise Refused("candidate did not report its private state version from the dry-adopt probe")


def wait_for_takeover(build, runs, after_ns=None):
    deadline = time.monotonic() + TAKEOVER_S
    waiting = dict(runs)
    next_adopt = 0
    while waiting:
        for rid, generation in list(waiting.items()):
            s = runner.load_state(rid)
            if s.get("run") != generation:
                raise Refused(f"{rid}: run generation changed during takeover")
            if s.get("state") == "ended":
                del waiting[rid]
                continue
            sup = s.get("supervisor") or {}
            if sup.get("code") != build or not sup.get("healthy") or not sup.get("pid"):
                continue
            if after_ns is not None and sup.get("completed_ns", 0) <= after_ns:
                continue
            identity = core.proc_identity(sup["pid"])
            if (identity == {k: sup.get(k) for k in ("pid", "boot_id", "ticks")}
                    and not runner.lock_free(os.path.join(runner.runs_dir(), f"{rid}.lock"))):
                del waiting[rid]
        if waiting:
            now = time.monotonic()
            if now >= deadline:
                raise Refused(f"takeover deadline expired for {', '.join(sorted(waiting))}")
            if now >= next_adopt:
                runner.adopt_runs(only=set(waiting))
                next_adopt = time.monotonic() + 30
            time.sleep(0.25)


def live_runs():
    runs = []
    for path in sorted(Path(runner.runs_dir()).glob("*.json")):
        if path.name.endswith((".job.json", ".admit.json")):
            continue
        lock_path = path.with_suffix(".lock")
        if path.stat().st_size == 0 and (not lock_path.exists() or runner.lock_free(str(lock_path))):
            continue  # An empty launch reservation has no supervisor to migrate.
        state = runner.load_state(path.stem)
        if state.get("state") in runner.NONTERMINAL and state.get("host") == core.HOST:
            runs.append(state)
    return runs


def check(flip):
    upgrade_lock = lock()
    if not upgrade_lock.acquire():
        return "skipped: another upgrade is in progress", 0
    try:
        host = core.load_conf().get("host")
        if not host or host not in (core.HOST, socket.gethostname()):
            return "skipped: not the configured board host", 0
        recovered = settle(flip)
        if recovered:
            return recovered, 1
        conf = settings()
        if conf.get("enabled") == "off":
            return "off", 0
        state = read_state()
        state["last_check"] = wall_time()
        try:
            upgrade_lock.change_mode(shared=True)
            result = update(state, conf.get("allow_major") == "yes")
            if isinstance(result, str):
                state["last_result"] = result
                core.write_private(state_file(), state)
                return result, 0
            build, pending = result
            if not upgrade_lock.change_mode(shared=False):
                state["last_result"] = "skipped: a run is starting"
                core.write_private(state_file(), state)
                return state["last_result"], 0
            runs = live_runs()
            active = {r["id"]: r["run"] for r in runs if r["mode"] == "systemd"}
            pending.update(time=wall_time(), runs=active)
            state["pending"] = pending
            state.pop("last_result", None)
            core.write_private(state_file(), state)
            ids = [r["id"] for r in runs]
            run(sys.executable, "-IB", os.path.join(build, "bin", "bus"), "_auto-upgrade-timer", "--help")
            run(sys.executable, "-IB", os.path.join(build, "bin", "bus"), "_auto-upgrade-recover", "--help")
            if probe(build, ids) != runner.STATE_VERSION and active:
                raise Refused("candidate changes private state version while a systemd run is active")
            flip(build)
            wait_for_takeover(pending["build"], active)
            probe(build, [r["id"] for r in live_runs()])
            entry = {k: v for k, v in pending.items() if k not in ("previous", "runs")}
            entry["time"] = wall_time()
            confirmed = dict(state, last_result=f"upgraded {pending['from']} to {pending['to']}",
                             upgrades=state.get("upgrades", []) + [entry])
            del confirmed["pending"]
            if "retries" in confirmed:
                confirmed["retries"] = {k: v for k, v in state["retries"].items() if k != pending["commit"]}
            core.write_private(state_file(), confirmed)
        except TagChanged as e:
            oid, commit = e.actual
            state.setdefault("seen", {})[e.tag] = dict(oid=oid, commit=commit, first=wall_time())
            state["last_result"] = f"failed: {e}; 24 h soak restarted"
            core.write_private(state_file(), state)
            return state["last_result"], 1
        except TagDeleted as e:
            state.setdefault("seen", {}).pop(e.tag, None)
            state["last_result"] = f"failed: {e}; cleared its soak observation"
            core.write_private(state_file(), state)
            return state["last_result"], 1
        except (OSError, Refused) as e:
            if "pending" in state:
                return rollback(state, flip, str(e)), 1
            state["last_result"] = f"failed: {e}"
            core.write_private(state_file(), state)
            return state["last_result"], 1
        return confirmed["last_result"], 0
    finally:
        upgrade_lock.release()


def update(state, allow_major):
    old = state.get("seen", {})
    refs = release_refs()
    now = wall_time()
    seen = {}
    for tag, (oid, commit) in refs.items():
        prior = old.get(tag, {})
        first = prior["first"] if prior.get("oid") == oid and prior.get("commit") == commit else now
        seen[tag] = dict(oid=oid, commit=commit, first=first)
    state["seen"] = seen
    core.write_private(state_file(), state)
    current = installed_release(refs)
    base = current.get("base", "")
    floor = version(base)
    if floor is None:
        raise Refused("installed build has no release version; upgrade once from a tagged checkout")
    newer = [tag for tag in refs if version(tag) > floor and (allow_major or version(tag)[0] == floor[0])]
    allowed = [tag for tag in newer if tag not in state.get("withdrawn", [])
               and refs[tag][1] not in state.get("quarantine", {})]
    eligible = [tag for tag in allowed if now - seen[tag]["first"] >= SOAK_S]
    if not eligible:
        if allowed:
            return "waiting for the 24 h soak"
        if newer:
            return "newer releases are withdrawn or quarantined"
        if any(version(tag)[0] > floor[0] for tag in refs):
            return "no eligible update (newer major needs opt-in)"
        return "no newer release"
    tag = max(eligible, key=version)
    oid, commit = refs[tag]
    retry = state.get("retries", {}).get(commit)
    if retry and now < retry["retry_at"]:
        retry_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(retry["retry_at"]))
        return f"{tag} retry after {retry_at}: {retry['reason']}"
    with tempfile.TemporaryDirectory(prefix="fetch-", dir=core.STATE()) as src:
        fetch(src, tag, oid, commit)
        try:
            build, withdrawn = stage_release(src, tag, commit)
        except Refused as e:
            raise Refused(record_failure(state, tag, commit, None, str(e)).removeprefix("failed: ")) from e
    state["withdrawn"] = sorted(set(state.get("withdrawn", [])) | set(withdrawn), key=version)
    before = current.get("tag") or f"{runner.installed_build()} (based on {base})"
    pending = {"from": before, "to": tag, "time": wall_time(), "commit": commit, "build": os.path.basename(build),
               "previous": runner.build_root(core.BIN())}
    return build, pending
