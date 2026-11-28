"""Check public release tags and activate verified builds through the installed bus symlink."""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from time import time as wall_time

from . import core, runner
from .core import Refused

REPO = "https://github.com/andylizf/agent-bus.git"
SOAK_S = 24 * 60 * 60
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
OID = re.compile(r"[0-9a-f]{40}")


def version(tag):
    match = TAG.fullmatch(tag)
    return tuple(map(int, match.groups())) if match else None


def git(*args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return run("git", "-c", "credential.helper=", *args, env=env)


def run(*args, env=None):
    try:
        proc = subprocess.run(args, env=env, cwd="/", capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(f"{args[0]}: {e}") from e
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise Refused(f"{args[0]} exited {proc.returncode}: {detail or 'no output'}")
    return proc.stdout.strip()


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


def checkout(src, tag, oid, commit):
    ref = f"refs/tags/{tag}"
    git("init", "--quiet", "--template=", src)
    git("-C", src, "fetch", "--quiet", "--depth=1", "--no-tags", REPO, ref)
    actual = (git("-C", src, "rev-parse", "FETCH_HEAD"), git("-C", src, "rev-parse", "FETCH_HEAD^{commit}"))
    if actual != (oid, commit):
        raise Refused(f"{tag} changed during fetch: expected {oid} ({commit}), got {actual[0]} ({actual[1]})")
    git("-C", src, "checkout", "--quiet", "--detach", commit)


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
    name = unit_name()
    env = "\n".join(f"Environment={json.dumps(key + '=' + os.environ[key].replace('%', '%%'))}"
                    for key in ("AGENT_BUS_BIN", "AGENT_BUS_CONFIG", "AGENT_BUS_STATE", "AGENT_BUS_BUILDS",
                                "AGENT_BUS_UNIT_PREFIX") if key in os.environ)
    executable = " ".join(json.dumps(p.replace("%", "%%").replace("$", "$$"))
                          for p in (sys.executable, "-IB", core.BIN()))
    unit = f"[Unit]\nDescription=agent-bus automatic upgrade\nConditionHost={fqdn}\n"
    (units / f"{name}.service").write_text(
        f"{unit}\n[Service]\nType=oneshot\n{env}\nExecStart={executable} auto-upgrade check\nTimeoutStartSec=15min\n")
    (units / f"{name}.timer").write_text(
        f"{unit}\n[Timer]\nOnActiveSec=15min\nOnUnitActiveSec=1d\n\n[Install]\nWantedBy=timers.target\n")


def lock():
    return runner.Locked(os.path.join(core.private_dir(), "upgrade.lock"))


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
        return "on (timer unavailable: needs a user manager and linger)" if enabled else "off"
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
    state = core.read_private(state_file()) or {}
    pending = state.pop("pending", None)
    if pending:
        if runner.installed_build() == pending["build"]:
            state["last_result"] = f"upgraded {pending['from']} to {pending['to']}"
            state.setdefault("upgrades", []).append(pending)
        else:
            state["last_result"] = f"failed: activation of {pending['to']} did not complete"
    return state


def settle():
    """Preserve the last auto-upgrade outcome before a manual switch moves its symlink."""
    if (core.read_private(state_file()) or {}).get("pending"):
        core.write_private(state_file(), read_state())


def doctor(user_manager, linger):
    enabled = settings().get("enabled") != "off"
    installed = runner.installed_build()
    current = runner.build_release(runner.build_root(core.BIN()))
    state = read_state()
    if not user_manager or not linger:
        timer = "unavailable (fallback: needs a user manager and linger)"
    elif not enabled:
        timer = "off"
    else:
        _, out, _ = runner.sysctl("is-active", unit_name() + ".timer")
        timer = out.strip() or "unavailable"
    return dict(enabled=enabled, timer=timer, current_tag=current.get("tag"), current_build=installed,
                base=current.get("base"),
                last_check=state.get("last_check"), last_result=state.get("last_result"))


def check(copy_build, flip):
    upgrade_lock = lock()
    if not upgrade_lock.acquire():
        return "skipped: another upgrade is in progress", 0
    try:
        conf = settings()
        if conf.get("enabled") == "off":
            return "off", 0
        host = core.load_conf().get("host")
        if not host or host not in (core.HOST, socket.gethostname()):
            return "skipped: not the configured board host", 0
        state = read_state()
        state["last_check"] = wall_time()
        try:
            result = update(state, conf.get("allow_major") == "yes", copy_build)
            if isinstance(result, str):
                state["last_result"] = result
                core.write_private(state_file(), state)
                return result, 0
            build, pending = result
            # Record the intended switch before the atomic flip. The symlink decides the outcome after a crash.
            core.write_private(state_file(), dict(state, pending=pending))
            flip(build)
            return f"upgraded {pending['from']} to {pending['to']}", 0
        except (OSError, Refused) as e:
            state["last_result"] = f"failed: {e}"
            core.write_private(state_file(), state)
            return state["last_result"], 1
    finally:
        upgrade_lock.release()


def update(state, allow_major, copy_build):
    current = runner.build_release(runner.build_root(core.BIN()))
    base = current.get("base", "")
    floor = version(base)
    if floor is None:
        raise Refused("installed build has no release version; upgrade once from a tagged checkout")
    old = state.get("seen", {})
    refs = release_refs()
    now = wall_time()
    seen = {}
    for tag, (oid, commit) in refs.items():
        prior = old.get(tag, {})
        first = prior["first"] if prior.get("oid") == oid and prior.get("commit") == commit else now
        seen[tag] = dict(oid=oid, commit=commit, first=first)
    state["seen"] = seen
    newer = [tag for tag in refs if version(tag) > floor and (allow_major or version(tag)[0] == floor[0])]
    eligible = [tag for tag in newer if now - seen[tag]["first"] >= SOAK_S]
    if not eligible:
        if newer:
            return "waiting for the 24 h soak"
        if any(version(tag)[0] > floor[0] for tag in refs):
            return "no eligible update (newer major needs opt-in)"
        return "no newer release"
    tag = max(eligible, key=version)
    oid, commit = refs[tag]
    with tempfile.TemporaryDirectory(prefix="fetch-", dir=core.STATE()) as src:
        checkout(src, tag, oid, commit)
        build = copy_build(src, release=dict(tag=tag, base=tag, commit=commit))
        run(sys.executable, "-IB", os.path.join(build, "bin", "bus"), "auto-upgrade", "--help")
    before = current.get("tag") or f"{runner.installed_build()} (based on {base})"
    pending = {"from": before, "to": tag, "time": wall_time(), "commit": commit, "build": os.path.basename(build)}
    return build, pending
