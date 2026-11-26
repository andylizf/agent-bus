---
name: agent-bus
description: Load before starting GPU work on a GPU host shared through an agent-bus v2 board, before ending a turn while such work runs, when a request, release or booking notice about such work arrives (a `MSG …` or `NOTICE …` line from `bus watch`, or a hook event), and before asking someone else's job for GPUs or booking GPUs there.
---

# Working on a GPU host shared through agent-bus

The host has no scheduler. The group shares it through a board directory and the `bus` command, which each member
installs for themselves with the setup block in https://github.com/andylizf/agent-bus/blob/main/README.md; `bus doctor`
checks the install. Your user is the person this session works for. A run's record on the board is its entry, under the
same id.

## The rules the tool enforces

- A run started without a booking is backfill, and it can be stopped at any time: by an authorized request from any
  member, and by any member's standing booking whose window starts on its GPUs. It has no GPU or time cap. A request
  is authorized when its file is the sender's own and `0644`, it names the run's current generation, and the run is
  not inside its own standing booking; any other request is shown and does nothing.
- A scheduled run is protected only inside its own standing booking's window. Book first with `bus book`, wait until
  the booking stands, meaning its objection window (15 minutes by default, 10 minutes minimum) closed with no reasoned objection, then
  start it with `bus run --scheduled <booking-id>`. Before the booking stands and after its window, it is backfill.
- A booking lapses if no run of yours is on it within 30 minutes of its start, and a lapsed booking protects nothing.
- A booking starts at most 7 days ahead and lasts at most 24 hours, and your bookings add up to at most 96 GPU-hours in
  any 7 days.

## Starting work

1. `bus cal` to see who is on which GPU and the bookings ahead; `nvidia-smi` for processes not on the board.
2. Backfill: `bus run --gpus <I> --hours <h> --kind <kind> --note "<coordination note>" -- '<command>'`.
   Protected: `bus book --gpus <I> --from <ISO time with offset> --to <ISO time> --kind <kind> --note "..."`, then
   once it stands, `bus run --gpus <I> --scheduled <booking-id> -- '<command>'`; `--exclusive` waits first until no
   process of anyone is on those GPUs.
3. Tell your user, in the message that reports the launch, the class and what it means for this run (backfill: it
   stops as soon as someone asks or books these GPUs).
4. Learn of releases: pass `--on-message <executable>` at launch, a program run with one JSON object on stdin per
   event (`request`, `stopping`, `released`, `ended`, `booking-objected` and the rest, each with the run's `id`), or
   run `bus watch <id>` as a background task, which prints the same events. The Claude Code stop hook, `bus stop-hook`,
   blocks a turn's end while a run of this session has no `--on-message` program and no `bus watch` running.

Work started later inside a running job goes through `bus exec <id> --run <generation> -- <command>`, the generation
being what `bus run` printed (`bus: entry <id> run <generation>`) and the entry's `run` in `bus ls --json`. The release
accounts for work started this way; start no GPU work in the job's environment by other means.

## Messages and events

- A request to your run: when it was authorized, the supervisor has already started the release; tell your user what
  was released and why. When it was not, nothing happens to the run.
- A booking of yours objected to: the booking will not stand. Tell your user the objection's reason and pick another
  window.
- A request your user wants to make: `bus send <entry-id> --kind request --gpus N --minutes M --note "..."`; replies
  arrive in your mailbox `<user>-mail` (your login name, then `-mail`), which `bus watch <user>-mail` prints. Object to a booking with `bus object <booking-id> --reason "..."`,
  only with your user's reason and during the booking's objection window.

Stop your own run with `bus release <id>`. A `kill` skips the check that the GPUs are free and the reply to whoever
asked.

## What goes on the board

Coordination facts only: GPUs, times, the kind (`training`, `eval`, `benchmark-timing`, `serving`, `interactive-dev`,
`other`), class, reasons in coordination terms. Never research content: models, datasets, methods, results, paths,
code, collaborators. Notes are at most 80 characters on runs and bookings and 200 on messages.
