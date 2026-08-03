---
name: session-protocol
description: Coordinate with other LLM sessions sharing one Unreal Editor — identity, roster, asking before disruptive work
version: 1.0.0
---

# session-protocol — Sharing One Editor With Other Agents

Several coding sessions can drive the same Unreal Editor at once. The bridge already tracks
every one of them from its first call — but under machine-generated names, and by default
no session ever looks. So one sits mid-PIE-test while another runs `build-and-relaunch`, and
the editor closes under it. This channel is how you look, and how you make yourself
recognizable when someone else does.

It is advisory. Nothing here blocks anything, no session can veto another, and there is no
lock to acquire. It tells you who else is here and what they are doing; the call is always
yours.

## Identity is per-command, not per-shell

Pick one short readable name at the start and reuse it for the whole session — `cape-cloth`,
`anim-repro`, `builder`. The name is what everyone else sees.

Carry it on every single call by prefixing the command itself:

```bash
SOFT_UE_SESSION=cape-cloth soft-ue-cli session list
SOFT_UE_SESSION=cape-cloth soft-ue-cli pie-session start
SOFT_UE_SESSION=cape-cloth soft-ue-cli build-and-relaunch
```

On the command line that prefix is the only form that works on every command, so use it for
everything — including commands that have nothing to do with this channel. On PowerShell, write it as
`$env:SOFT_UE_SESSION="cape-cloth"; soft-ue-cli pie-session start`.

A one-off prefix is not `export`. `export SOFT_UE_SESSION=...` on its own line does not
work: each Claude Code Bash call gets a fresh shell, so the variable is gone by the next
command. There is no session-scoped place to store your name — if you omit it, you register
under a derived label and your earlier entries look like a different session.

`--as <name>` also sets your identity, but it exists **only** on the seven `session` leaves
(`announce`, `list`, `broadcast`, `ask`, `answer`, `inbox`, `leave`). Anywhere else it is an
argparse error, not a no-op: `soft-ue-cli pie-session start --as cape-cloth` simply fails.

### Mixing the two forms splits you into two roster rows

The roster is keyed on the identity each individual call carries. So `session announce --as
cape-cloth` followed by a bare `pie-session start` registers two separate sessions:

| roster row | what it holds |
|------------|---------------|
| `cape-cloth` | your name, status, intent, declared resources |
| `unknown:<origin>` | your `pie` claim — the one thing others are looking for |

Nothing errors. You just become invisible in exactly the way that matters: whoever runs
`session list --resource pie` finds a nameless row they cannot address. And on your side,
`session inbox --as cape-cloth` never shows messages sent to the derived row, and
`session leave --as cape-cloth` retires only the named one — the row holding `pie` is left
to age out on its own.

Prefix every command with `SOFT_UE_SESSION` and there is only ever one row.

Through MCP there is no shell to prefix, so say it once as a tool parameter instead: pass
`session_as` to the `session announce` tool and that name becomes the default identity for
every later tool call from the same server, `pie-session` included. Passing `session_as` to
any other `session` tool still means "act as, for this one call" and changes nothing after it.

## Derived versus declared

Most of what others see about you costs you nothing. Every bridge call you make refreshes
your entry's `last_seen_s` and `last_tool`, and PIE state is derived directly from
`pie-session start` / `pie-session stop`. You are already on the roster before you announce
anything.

`session announce` is only for intent the bridge cannot observe:

```bash
SOFT_UE_SESSION=cape-cloth soft-ue-cli session announce \
  --status "Converting SK_Cape to Chaos cloth" \
  --intent write \
  --resources /Game/Characters/SK_Cape,/Game/Characters/PA_Cape
```

`--intent` is one of `read`, `write`, `pie`, `build`, `editor-restart`. Announce once when
you start, and again when what you are doing changes materially. You do not need to announce
to be visible, and you do not need to re-announce before each command.

## Reading the roster

```bash
SOFT_UE_SESSION=cape-cloth soft-ue-cli session list
```

Returns `sessions`, plus `you` (your own id) and `now_utc`. Each entry carries `label`,
`status`, `intent`, `resources`, `last_tool`, `last_seen_s`, and a `state` grade.

`intent` is only ever what a session typed into `announce`. `resources` is the one that
fills in on its own: `pie-session start` claims the literal resource `pie`, so a session
running a test carries `pie` in `resources` whether or not it announced anything.

| `state` | What it means | How to treat it |
|---------|---------------|-----------------|
| `active` | Made a bridge call moments ago | Genuinely busy. Disrupting this interrupts real work. |
| `idle` | Alive but quiet for a while | Probably thinking or editing files. Worth asking. |
| `stale` | Silent for 15+ minutes | Usually a dead terminal, not a real blocker. |
| `ended` | Called `session leave` | Gone. Ignore it. |

`stale` and `ended` entries are hidden unless you pass `--include-stale`. Filter with
`--resource <path>` to see only sessions touching an asset you are about to modify, or
`--resource pie` to find who would lose a running test. Do not reach for `--intent pie`
here: it matches only sessions that declared that intent by hand, so it silently misses
every session that just ran `pie-session start` — the ones you most need to see.

## Asking before disruptive work

Before anything that restarts the editor, rebuilds, stops PIE, or rewrites an asset someone
else declared: look at the roster, and if a session is `active` or `idle`, ask it.

```bash
SOFT_UE_SESSION=builder soft-ue-cli session ask --to cape-cloth \
  --question "I need to rebuild and relaunch the editor. Can I?" \
  --context "SoftUEBridgeEditor changed" \
  --timeout 120
```

`--to` takes another session's name, or `all`. The reply comes back with a `--decision`:

- `yes` — proceed.
- `no` — do not. Ask what they need, or do something else and come back.
- `wait` — re-ask after the time they state. Do not treat it as a soft yes.

Answering one is symmetric — the `ask_id` arrives in your inbox:

```bash
SOFT_UE_SESSION=cape-cloth soft-ue-cli session inbox
SOFT_UE_SESSION=cape-cloth soft-ue-cli session answer --id a-3 \
  --answer "Give me 10 minutes, I am mid-PIE" --decision wait
```

An ask expires the moment it is answered, so only the first answer lands.

## Silence is not consent

No answer is the most common outcome, not permission.

The other session only sees your question when its own next bridge call returns. A session
mid-thinking, mid-build, or mid-file-edit makes no bridge call for minutes, so it is not
ignoring you — it simply has not looked yet. Without `--timeout`, `ask` returns immediately
with an `ask_id`; with `--timeout`, it polls and then gives up.

When nobody answers, do not wait indefinitely, and do not read the silence as a yes. Branch
on the silent session's `state`:

- `stale` — almost certainly a dead terminal. Proceeding is reasonable.
- `idle` — quiet but alive. Consider a `session broadcast --tag warning` and a short wait.
- `active` — genuinely busy and will probably see your question soon. This is the one worth
  waiting on, or worth routing to the human.
- **absent** — the session you asked is in neither `silent` nor `session list`. Both hide
  `stale` and `ended` entries, so a target that went stale while you waited does not appear
  anywhere with a grade. Absence is not a fourth state: re-run
  `session list --include-stale` to find out whether it went `stale` (dead terminal) or
  `ended` (left cleanly) before you treat it as gone.

If you decide to proceed over a session that was `active` or `idle`, say so plainly in your
output — name the session and what you did — so the human can see who got interrupted and
why. A no-answer result includes the `silent` roster and a `guidance` line for exactly this
decision.

## Leaving

```bash
SOFT_UE_SESSION=cape-cloth soft-ue-cli session leave --reason "cloth conversion done"
```

This is the only clean end-of-session signal. Without it your entry ages out to `stale` on
its own, and for the next 15 minutes everyone else has to guess whether you are still there.
Calling `leave` twice is harmless.

## What this channel cannot see

It only sees destruction that goes through the bridge. These are invisible to it, and the
roster will still show sessions as `active` right after the editor is gone:

- `taskkill` or any other direct process kill
- the editor's own Compile / Live Coding button
- running `Build.bat`, `RunUAT`, or the IDE's build directly
- an editor crash

So a clean roster is evidence about bridge traffic, not a guarantee the editor will survive
the next minute. When the editor does vanish, your next bridge call fails with a post-mortem
naming the session whose `build-and-relaunch` or restart took it down — if that action went
through the bridge.

## Other commands

`session broadcast --message "<text>" --tag fyi|warning|request` tells everyone
something without expecting a reply. `session inbox` reads messages and open
questions addressed to you; `--wait <sec>` polls, `--unread-only` skips what you have seen,
`--no-mark-read` leaves your cursor alone. You never receive your own messages back.
