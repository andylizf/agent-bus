# agent-bus

A shared board for people and coding agents who use one GPU host with no scheduler. Each member starts jobs with
`bus run`, which runs the job under their own systemd user manager and puts an entry on the board: which GPUs, until
when, and whether the job is protected by a booking. Anyone can book GPUs ahead with a public notice, object to a
booking with a reason, or ask a running job for its GPUs. The tool enforces the outcome on each member's own jobs: a
job started without a booking stops when someone asks for its GPUs or when someone's booking of them begins, and the
asker is told only once the GPUs are free of that job.

There is no server. The board is a directory on a filesystem every node mounts (GPFS, NFS, Lustre), it holds data
only, and nobody runs code from it: each member installs `bus` privately from a commit they chose. Python 3.9,
standard library only. The first version of the tool, which ran from the board directory itself, is in this
repository's history at `bff1824`; this version starts a new board and does not read or write the old one.

## How it works

### Runs and their supervisor

`bus run --gpus 2,3 -- '<command>'` starts the job as a transient systemd user service in a slice of its own, so
every process the job forks stays accounted for, through `setsid`, double forks and daemonizing launchers such as Ray
alike. An Apptainer instance that systemd moves into a scope of its own is still found, through its parent in the
slice, and released with the job. A run's record on the board is its entry; run and entry share one id. Each run also
gets a generation, a random hex id that `bus run` prints (`bus: entry <id> run <generation>`) and the entry carries as
`run`, so a message meant for an earlier run under the same id is told apart. A per-run supervisor, another user service, checks the board every 5 seconds, applies the
rules below to its own job, and touches the entry's heartbeat. The job survives the client, the supervisor and the ssh
session that started it. The client waits in the foreground and exits with the job's status, or 75 when a release
ended it; `--detach` returns at once. `--on-message <executable>` names a program the supervisor runs with one JSON
object on stdin for each event of the run (`request`, `release-started`, `stopping`, `released`, `ended`,
`leader-exited`, `needs-reconcile`, `exec-unidentified`, `booking-ahead`, `booking-objected`,
`board-identity-changed`), killed after 10 seconds. `bus watch <id>` prints the same events.

An entry whose heartbeat is 120 seconds old shows as "occupancy unknown": its GPUs count as neither claimed nor free,
and the live `nvidia-smi` view says what is on them.

### Two classes

A booking stands once its objection window closes with no objection; only then does it protect anything. Every stop
below is the verified release described further on: TERM, then KILL, then a check that the GPUs are free of the job.

| | Scheduled | Backfill |
|---|---|---|
| Started as | `bus run --scheduled <booking-id>` | `bus run` (the default) |
| Needs | your own standing booking covering the job's GPUs and now | nothing |
| Protected | inside its booking's window: nothing on the board stops it | never |
| Stops when | you release it; once its window ends it is backfill | an authorized request; a standing booking's window starting on its GPUs |
| Limits | its booking's (below) | none: any free GPUs, for any length |

A scheduled run whose booking is pending, objected, lapsed or past its window is treated as backfill until the booking
stands again; it never yields to its own booking. A new backfill run is refused only on GPUs that are inside another
booking's standing window right now; a pending booking does not block it.

### Bookings as public notices

`bus book --gpus 0-3 --from 2026-11-24T22:00-05:00 --to 2026-11-25T01:00-05:00` publishes a booking. Silence is
consent: the booking stands once its objection window closes (15 minutes by default, never under 10) unless someone
publishes an objection with a reason, `bus object <booking-id> --reason "<why>"`. The board and its read-only page
(`bus page`, below) show the booking. When a pending or standing revision is published that can take effect on a
backfill run's GPUs before the run's stated end, the run's supervisor emits `booking-ahead` once for that revision
via the run's hook. When a standing booking takes effect, every backfill run on its GPUs releases, including the
booker's runs.

Every booking revision and every objection gets a receipt in an append-only sequence under `seq/`. The slot is claimed
by `link(2)`, so the filesystem's `EEXIST` decides the order and no writer picks its place. An earlier receipt wins an
overlap: a later booking of another member on the same GPUs and time is `conflicting`. A booking's objection window
counts from its receipt, so a writer cannot shorten it by backdating the booking.

Limits, enforced by the tool when you publish and by every reader that acts on a booking:

- a booking starts at most 7 days ahead and covers at most 24 hours, with an objection window of at least 10 minutes;
- budget: at most 96 GPU-hours of your pending, standing or lapsed bookings in any 7-day span; bookings past it are
  `over-budget`;
- no-show: a standing booking with no run of its owner on it within 30 minutes of its start, or of its objection
  window closing if that is later, is `lapsed` for the rest of its window and protects and evicts nothing.

A booking's state is one of `unpublished`, meaning it has no receipt, `invalid`, `over-budget`, `conflicting`,
`objected`, `pending`, meaning its objection window is open, `standing` and `lapsed`. Rewriting a booking with `bus book --revise <id>`
makes a new revision with a new objection window, except a revision that only narrows a standing one to fewer GPUs or
a shorter window inside the old one, which stays standing.

Booking times are ISO 8601 with a UTC offset, or epoch seconds.

### Requests

`bus send <entry-id> --kind request --gpus 1 --minutes 30 --note "eval before the deadline"` asks a run for its GPUs.
A request stops a run only when all of these hold: the message file is `0644` and owned by the user it names as
sender, its `reply_to` is a mailbox that sender owns, it names the run's current generation (so a request meant for an
earlier run with the same id does nothing), its id has not been seen before, the board is the one the owner pinned at
install, and the run is not inside its own standing booking window. Anything else is shown and acts on nothing.

### Verified release

A release, whether from a request, a booking's start or `bus release <id>`, closes the run to new `bus exec` work and
waits for any exec already admitted. It then sends TERM to the slice and to each of the job's processes that left the
slice, such as an Apptainer instance that systemd moved into a scope of its own, each through a pidfd checked against
the process recorded for it, and after 15 seconds KILLs them. A unit is never signaled as a whole, so a process that
merely shares one with the job is left alone.

"Released" is reported only when the slice is gone or unpopulated, no recorded process of the job is alive, and
`nvidia-smi` answers and shows no process of yours on the run's GPUs except those another live run of yours owns by
its own records. Until then the requester reads "stopping, not yet free" with what remains; a failed `nvidia-smi`
query counts as not free. A process of yours on one of the GPUs that no run's records explain puts the run in
`needs-reconcile`: the GPUs without such a process are released and reported free of this job, the others stay
claimed, and you either stop that process or confirm it is not this run's with `bus reconcile <id> --confirm <pid>`.

Work started later inside a running job joins only through `bus exec <id> --run <generation> -- <command>`, which
admits it before its scope starts in the slice; inside the new scope the command runs only if that admission still
stands. An exec that is paused or slow keeps the run "stopping" until it has either started inside the slice (and is
stopped with it) or both its client and its launcher are gone; there is no timeout shortcut.

### Cleanup after the job leader exits

When the leader of a systemd run exits, `bus` saves its exit status and sends TERM to processes in the job's
original cgroup. It rechecks each PID's membership just before signaling. A process that moves to another scope
between that check and delivery can still receive TERM. If processes remain after a 10-second grace period,
the kernel's `cgroup.kill` interface sends KILL to the job cgroup. The run log records each TERM with its PID
and command name. It also records the `cgroup.kill` request and a snapshot of PIDs and command names taken
just before it.

The run holds its GPUs while cgroup cleanup is unverified or the slice repopulates. Before reporting `ended`,
the supervisor checks GPU release and confirms on later passes that the job service and its slice have stopped.
A missing or unreadable required launch record or a different systemd invocation keeps all GPUs held in
`needs-reconcile`. If the job cgroup is still present and the host's boot ID or the cgroup inode differs from
the launch record, the run stays in `stopping`. `bus state <id>` shows what is holding the run.

Runs adopted from v2.0.7 lack a launch-time cgroup identity record. The supervisor holds their GPUs while the
job cgroup is populated. Once it is empty, the supervisor can verify GPU release and reclaim the units. The
owner can use `bus release <id>` to stop an older run through the explicit release path.

### Trust

- A record's author is its file owner. An entry, booking, objection, receipt or message counts only when its owner's
  name equals its `user` (or `sender`) field and its id starts with `<user>-`. Records are `0644`. Sticky
  directories let a record's author or the directory owner remove it. A node-wide board admits any local
  account. A board created with `--group` limits writes to that group and uses setgid for group inheritance.
- Every path on the board is opened by an `openat` walk from `/` with `O_NOFOLLOW`; readers take only regular files
  of at most 64 KiB, skip dot names and symlinks, and skip a directory of more than 20,000 entries.
- Each install records the board directory's inode and filesystem id. If the directory at that path is ever
  replaced, every `bus` command still reads it (shown as "board identity changed"), and supervisors write nothing to
  it and let nothing on it act on their jobs; the jobs keep running and their owners are told.
- The owner of a pinned board can authorize a new board path through `move.json`. The client opens every
  component without following symlinks and checks the destination's owner, inode, filesystem id and host.
  Ordinary records supply no write paths, signal targets, SSH hosts or shell words.
- Any account with node access can now use the actions below. Sticky directories protect existing names from
  most other accounts, but anyone with write access can take vacant receipt or inbox names and fill shared
  directories. The board's XFS has no quota, so raw filesystem writes can consume inodes. The client caps
  each sender at 128 messages per inbox per hour. It checks receipt-block ownership and bounds collision retries.
  These checks do not count or limit raw filesystem writes. The client relies on record ownership and modes
  directly, independent of the runtime `fs.protected_*` settings.
- The source GPFS parent is group-writable and lacks the sticky bit. A group member can rename a child there and
  interrupt a move. Clients require the exact pinned inode before trusting any move record.

What any member can cause, by design: a request stops a backfill run within one supervisor pass; a standing, unlapsed
booking stops every backfill run on its GPUs when its window starts; a reasoned objection during a booking's
objection window keeps it from standing. Nothing stops a scheduled run inside its standing window.

### Adoption and updates

A per-user timer (`bus-adopt.timer`, every 30 seconds, on the board's host only) starts a supervisor for any run of
yours that has none, so a killed supervisor is replaced within about 30 seconds while its job keeps running.
`bus upgrade --build <dir>` installs a new build and points `~/.local/bin/bus` at it; each supervisor saves its state
and execs the new build at the end of its next pass. Rolling back is the same command with the previous build.

Without a user manager, as when linger is off, `bus run` supervises the job itself in the foreground. This fallback
mode has no adoption, no updates and no `bus exec`, and a release that finds a descendant which left the job's
process group waits until `bus reconcile <id>` confirms or the owner stops it. `bus doctor` says which mode you are
in. Rolling back from a build that does not start uses the previous build's own entry point:
`~/.local/share/agent-bus/builds/<previous>/bin/bus upgrade --build ~/.local/share/agent-bus/builds/<previous>`.

### Automatic upgrades

Every install enables automatic upgrades by default. On the board host, `bus-auto-upgrade.timer` checks the
public `andylizf/agent-bus` Git repository about 15 minutes after the timer starts and daily after that. Each
account waits at least 24 hours from when it first sees an exact `vX.Y.Z` tag object and commit. If a check sees a
tag change or disappear, it starts a fresh wait when the tag returns, even when it returns to the same commit.
The updater picks the newest eligible version. It stays within the installed major version unless you run
`bus auto-upgrade on --allow-major`, and it requires a higher version in either mode.

The updater builds from regular files in the verified Git commit. The previous build drives the switch and
checks that the candidate can load existing run state. The takeover budget is 13 minutes: six minutes each for
the old pass and the new pass, plus one minute for re-exec or adoption. A pass can spend up to two minutes on
serial GPU queries, with the rest of the allowance for systemd, admission, and hook waits. An existing run can
report a healthy pass from its re-executed supervisor or from a supervisor adopted by the new build.

The timer and `bus auto-upgrade check` start the same systemd service. Both user services follow the installed
`bus` entry point. While a switch is pending, the launcher uses the previous build for recovery. A failed or
interrupted switch restores the previous build and waits for every live run to complete a healthy pass on that
build after the switch back. This also
works with `--no-timer`, which skips periodic adoption and leaves automatic upgrades enabled. If recovery
cannot verify a supervisor, `bus doctor` reports which run is waiting. The adopt service can resume recovery
after 18 minutes. Failed commits can retry after 15 minutes and then one hour. A third failure quarantines the
commit. Rolling back to a build that predates automatic upgrades restores adoption and leaves upgrade checks
idle. New launches share the updater's lock during download. The updater takes it exclusively before switching
builds, so a launch already in progress defers the switch.

To recall a release, remove its tag from the public repository and publish the replacement under a higher version.
A release can also include a root-level `auto-upgrade-withdrawn` file listing older `vX.Y.Z` tags, one per line.
Once an account checks the newer verified release after its soak, it remembers those withdrawals even if takeover
fails. A recalled tag stops being a candidate for that account. An account already running it waits for a higher
eligible version.

Anyone who can push tags to `andylizf/agent-bus` can run code in every account with automatic upgrades enabled
after the 24-hour soak. To opt out at install, pass `--no-auto-upgrade` to `bin/install.sh`. For an existing
install, run `bus auto-upgrade off`. Run `bus auto-upgrade on` to turn it back on with the same-major rule.

For an install on v2.0.0 through v2.0.8, fetch and check out a release that includes automatic upgrades in
`~/src/agent-bus`, then run this one-time command on the board host:

```sh
~/src/agent-bus/bin/bus upgrade --build ~/src/agent-bus
```

The checkout's `bus` sets up the timer as part of the switch. If the installed older `bus` performs the upgrade,
the new build sets up the timer and creates `release.json` on its first invocation. Its first check identifies the
installed tag from the commit and installed files. `bus doctor` shows the current tag or build ID, timer
availability, last check, result, retry, and quarantine reasons. The timer requires a systemd user manager with linger
enabled. Run `loginctl enable-linger "$USER"`, then `bus auto-upgrade on` to start the timer when it was
unavailable. Automatic upgrades record the source version, destination tag and time in
`~/.local/state/agent-bus/auto-upgrade.json`.

Each supervisor publishes its running tag or build ID in the entry's `build` field and in the heartbeat file
under `agents/.heartbeat/`. Use `bus ls --json` to see the version on each running entry. The heartbeat's
modification time records when the supervisor last checked in.

On the board host, run `python3 tools/auto_upgrade_gate.py` from the release checkout to verify rollback with
systemd. The gate creates a temporary board, a sleep job, and two candidates. It checks the same job invocation
and PID throughout, a fresh heartbeat from the restored build after a forced failure, adoption through the service
launcher, and a new-version heartbeat after a successful takeover. Cleanup uses the exact unit names and
invocation IDs recorded by the gate.

## Setup

Run once, on the GPU host, as yourself (or let your agent run it):

```
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger                 # expect: Linger=yes
git clone https://github.com/andylizf/agent-bus ~/src/agent-bus && git -C ~/src/agent-bus checkout <release tag>
~/src/agent-bus/bin/install.sh
systemctl --user enable --now bus-adopt.timer
systemctl --user list-timers bus-adopt.timer --no-pager   # expect a NEXT time
bus doctor                                           # expect: linger yes, user manager yes, cgroup2 yes, slice test ok, board pin ok
```

`install.sh` copies the checkout's build to `~/.local/share/agent-bus/builds/<commit>/`, points `~/.local/bin/bus`
at it, pins the board in `~/.config/agent-bus/board.conf`, writes the adopt timer with `ConditionHost=` (home
directories shared across nodes would otherwise start it everywhere), and prints the Claude Code stop hook to add.

Releases are the repository's tags (`git -C ~/src/agent-bus tag`). The default board is `/scratch/agent-bus-v2`.
Its root and config must belong to `al9080` on `della-tridao`. Setup accepts a protected empty root owned by
that account, so the owner can create it before advertising the path. For a different board path, pass
`--board <board-dir> --board-owner <account>` to `install.sh` on its host. Omit the owner for a board you own.
The timer uses the verified local hostname.

### Move an installed board

The move build ships through the normal release channel. Each account's 24-hour soak starts when it sees the
release. After the upgrades, have `al9080` run the move during an empty window on `della-tridao`.

```sh
OLD=/scratch/gpfs/TRIDAO/agent-bus-v2
NEW=/scratch/agent-bus-v2
bus move-board "$OLD" "$NEW"
```

The command lists blockers and leaves the source active. Retry when there are zero runs, zero pending or
standing bookings, and zero open objection windows. Each run inbox needs its own archived end record.
The owner can use `bus gc --mine` when the same run's private state confirms it ended or the launch was
interrupted before admission. GC checks the inbox or its matching preparation state and waits at least two
minutes. It closes private admission and verifies that no exec client, launcher or run unit remains before
recording that a run never started.
Unreceipted future booking revisions also block. Each account that published in the past 14 days must report
a confirmed move-capable build. Board writes, supervisor passes and automatic timers report the build.
Allow up to 30 seconds for other clients to observe a new receipt when checking its objection deadline.

The move takes the source write lock, seals board-owned publication directories and mailboxes, and checks again
before creating the new board with the same host, GPU count and booking limits. Setup can use an empty protected
destination directory already owned by `al9080`. The owner signs the new pin with `move.json` and archives the
original root at `$OLD.retired-<inode>`. Unix keeps the modes of other users' mailboxes under their owners' control.
An old client mid-send may still report success when it writes to another user's archived inbox through an open
descriptor. The message stays in the archive. Keep the GPFS archive in place and treat it as read-only.

The installed, confirmed build re-pins on its next board access. Entries, bookings, objections, arrivals,
receipts and messages stay in the archive. The new board stores each charged booking interval and its GPU
count in owner-authenticated metadata. Each charge remains in the rolling budget until seven days after its
own end, including bookings that lapsed after a no-show. The new board starts a fresh receipt sequence.
The new page starts with no Agent Bus entries or bookings and continues to show live GPU telemetry.

Before cutover, the source remains active. After an interruption, rerun the same command with the same paths.
To move again after cutover, wait for an empty window and use a fresh destination, keeping the original archive.
Let confirmed clients re-pin before a second move. An installation two moves behind needs to install at the
current path. Use GPFS only if all authors can access the destination.

## Commands

| Command | Does |
|---|---|
| `bus run --gpus I [--scheduled B \| --hours H] [--exclusive] [--kind K] [--note T] [--stop-cost-min M] [--on-message E] [--mem-max MB] -- '<cmd>'` | start a job (backfill unless `--scheduled`) |
| `bus exec <id> --run <gen> -- <cmd>` | run a command inside a running job's slice, through admission |
| `bus release <id> [--wait S]` | release your own run, verified |
| `bus ls [--json]`, `bus cal [--days N] [--json]` | runs on the board, bookings and their states |
| `bus book ...`, `bus unbook <id>`, `bus object <id> --reason ...` | bookings and objections |
| `bus send`, `bus reply`, `bus mailbox <name>`, `bus watch <id>` | requests, answers, your mailbox `inbox/<user>-<name>` (replies to your requests go to `<user>-mail` unless you name another); `watch` prints new messages and your run's events and writes nothing |
| `bus state <id>`, `bus runs` | your runs' private state |
| `bus gc --mine` | your runs no supervisor holds, and your entries whose run has ended |
| `bus reconcile <id> [--confirm <pid>...] [--cancel-exec <exec-id>]` | list or confirm processes a release cannot attribute to the run; clear an admitted `bus exec` whose client is gone and whose launcher cannot be identified, named by the `exec-unidentified` event |
| `bus doctor`, `bus upgrade --build <dir>`, `bus adopt --all`, `bus stop-hook` | install checks, updates, adoption, the Claude Code stop hook |
| `bus move-board <old> <new>` | archive an empty board and create its replacement, as the board owner |
| `bus page [--port 8300]` | a read-only page of the board, served on 127.0.0.1 from your own install |

### B300 board on the web

The [B300 board](https://b300-board.github.io/) polls the [public feed](https://cc.zhifei.li/b300/board.json)
every 15 seconds. The feed contains GPU memory and utilization, the NetID of everyone with a process on a GPU, run windows and booking states.
The page shows the [latest snapshot](https://b300-board.github.io/snapshot.html) if the feed fails and marks
readings stale after two minutes.

On mac-mini, `io.github.andylizf.b300-board-live` runs `tools/board_feed.py`. It collects one reading over a
shared SSH connection and sends the JSON to the upload-only SSH command `tools/board_upload.py`.
`io.github.andylizf.b300-board-publish` runs `tools/publish_pages.py` every ten minutes to publish
`snapshot.json` with the static shell. Build the shell with `python3 tools/publish_pages.py --build-shell DIR`.

`kind` is one of `training`, `eval`, `benchmark-timing`, `serving`, `interactive-dev`, `other`. `--hours` is the
expected length shown on the board (24 by default; it limits nothing). `--mem-max` caps the job's memory as systemd's
MemoryMax with no swap, so a job that goes over it is killed by the kernel. `--exclusive` on a
scheduled run waits, before starting, until every backfill run on the booked GPUs has released and no process of
anyone is on them.

## What goes on the board

Entries, bookings, objections and messages carry coordination facts only: GPUs, times, the kind from the list above,
class, the owner's estimate of minutes of work a stop would lose (`--stop-cost-min`), and reasons in coordination
terms. They never carry research content (models,
datasets, methods, results, paths, code, collaborators). Notes are optional and capped at 80 characters on entries
and bookings and 200 on messages.

## Tests

`python3 tests/run_tests.py -j 4` runs the v2 suite, each test in its own process; set `AGENT_BUS_TEST_ROOT` to a
directory on the board's filesystem to run it there. Tests that need a systemd user manager skip without one.
`tests/container/` builds a Rocky Linux 9 container with systemd and two users (`up.sh`), and `rehearsal.sh` runs
the setup block for both and checks the rules above across users.
`tests/della/cross_node.sh` checks receipt claims and `lockf` from two nodes at once.

## License

MIT
