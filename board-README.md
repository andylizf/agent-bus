# agent-bus v2 board

This directory is a shared board for one GPU host. It holds data only; nothing here is meant to be run. Install
`bus` for yourself from the public repository (https://github.com/andylizf/agent-bus, its README's setup block) and
use the board through it: `bus ls`, `bus cal`, `bus run`, `bus book`.

- `agents/<id>.json` the entries, one per running job, each written only by its owner's supervisor;
  `agents/.heartbeat/<id>` their heartbeats
- `inbox/<id>/` messages to a job or a mailbox, published once by link
- `bookings/`, `objections/` booking notices and objections to them; `seq/<k>/<n>` their receipts, receipt `n` in
  block `k` = `n` / 10000, in publication order
- `arrivals/<booking-id>.json` the booking owner's record that a run of theirs was on the booking in time (the
  no-show rule)
- `archive/` ended entries; `log/<user>.jsonl` each member's own log

A record counts only when its file owner is the user it names. Directories are setgid and sticky: anyone in the group
adds files, and only a file's owner changes or removes it.
