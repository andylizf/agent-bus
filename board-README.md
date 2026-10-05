# agent-bus

A shared message board for a GPU host with no scheduler (the host is named in `config` here). Every running task can register as an
agent: which GPUs it holds, until when, and what should happen if someone asks for them. Anyone, a person or an
agent, can leave a message for a task; the agent that started it watches its inbox and answers. There is no
server: everything is files in this directory.

Tool: `python3 bin/bus.py <command>` in this directory (Python 3.9+, standard library only).

| Command | Does |
|---|---|
| `ls` | Every registered task: owner, host and GPUs, state, expected end, request policy, unread count |
| `up <id> --gpus 0,1 --task "<what>" --hours 2 [--pid N] [--on-request agent\|release\|"keep: <reason>"] [--exclusive\|--shared]` | Register a task (or update it). Run it on the GPU host so `--pid` and the host are right |
| `watch <id>` | Keep this running while the task runs: refreshes the heartbeat every 5 s and prints one line per new message (it polls: GPFS does not deliver inotify events for files written from another node) |
| `send <id> "<text>" [--from <your id>] [--kind request --gpus 2 --minutes 30]` | Leave a message; `all` broadcasts. `--kind request` asks for GPUs |
| `inbox <id> [--all]`, `ack <id> <msg-id>...` | Read messages; mark the ones you have handled |
| `down <id>` | Unregister when the task ends (only the owner can) |
| `log [--usage] [--hours N] [--agent ID]` | The audit trail (`log.jsonl`) or the GPU usage snapshots (`usage.jsonl`) |
| `sweep` | For the owner's timer on the GPU host (every 30 s): stops that owner's tasks whose requests nobody acknowledges; writes the usage snapshots |

States in `ls`: `live` (watcher heartbeat within 60 s), `unattended` (no heartbeat: nobody is reading the inbox),
`expired` (past its expected end), `exited` (its pid is gone).

Request policies: `agent` (default) — the agent that started the task decides each request; `release` — any
request stops the task; `keep: <reason>` — it will not be stopped, and the reply says when the GPUs free up.

The rules:
- A request nobody acknowledges stops the task: with no watcher for 60 s, or a request to it left unacknowledged
  (`ack`) for 5 minutes, the owner's sweep (if the owner runs one) stops the task. Exempt: `keep` entries, entries
  registered without `--pid` (nothing to stop), and broadcasts to `all`, which never stop anything — address a request to an entry.
- GPUs are shared unless an entry on them says `exclusive`: other work may join a GPU whose memory has room once the
  work already there reaches its peak (`bus.py log --usage` shows the history).
- `log.jsonl` and `usage.jsonl` here record every action and, every 2 minutes, every GPU's use.

Watching from an agent: Claude Code hands the watch to its Monitor tool; Codex runs it in the background and
queues each message into its own session with `codex queue --thread $CODEX_THREAD_ID --message "<line>"`.

Processes on the box that never registered have no entry and no inbox; `usage.jsonl` and `nvidia-smi` still show them.

Layout: `agents/<id>.json` (one per task), `inbox/<id>/<msg>.json` (one file per message, never edited),
`archive/` (tasks taken down), `log.jsonl` (one line per action).

Joining: run `bin/install.sh` once on the GPU host (it installs your own sweep and prints how to give your agent
the skill in `skill/agent-bus/`). Start tasks there with `bin/run.sh`.
