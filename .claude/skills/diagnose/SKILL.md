---
name: diagnose
description: Use when something on a warpzone machine seems wrong (a console does not receive games, saves do not sync, the updater or timer looks stuck, a tool is missing, a command fails) or when the user asks for a health check. Runs warpzone doctor and health, interprets the output and proposes fixes.
---

# Diagnose a machine

Read-only by default. `doctor` never installs or changes anything and makes no network calls (apart from one request to the local Syncthing on loopback). `health` writes nothing. Fixes that change files or services are proposed to the user and run only after their OK. References: [doctor.md](../../../website/src/content/docs/operation/doctor.md), [self-update.md](../../../website/src/content/docs/operation/self-update.md#checking-a-version).

Never print API keys or the config's secrets. Show the commands you ran and the relevant output.

## Steps

1. Run on the machine that has the problem (a console, or the desktop that runs `inbox`):

   ```bash
   warpzone doctor            # add --config PATH for a config in another place
   warpzone health <device>   # on a device; the name is a [devices.<name>] table of the config
   ```

   Without a device name `health` picks the one whose ID matches the local Syncthing. If `warpzone` is not on `PATH` on a device, use `~/.local/share/warpzone/venv/bin/warpzone`.
2. Read the markers: `[ok]` fine, `[! ]` warning, `[!!]` problem, `[--]` optional and not set up (not a problem). `doctor` exits `1` only when something required is broken. `health` ends with `health: ok` (exit `0`) or the number of failed checks (exit `1`).
3. For each `[!!]` and `[! ]`, quote the line and its `fix:` text, explain it in one or two sentences, and rank by impact. A tool or table the user does not use shows as optional; do not push to set it up.
4. Propose fixes. Run a fix only after the user agrees. Common ones, from the docs:

   | Symptom | Usual fix |
   |---|---|
   | Missing inbox tool or DATs | the install command printed under `fix:` |
   | ES-DE startup script, artwork timer, play-state timer, shadPS4 launcher missing or stale | `warpzone provision device <device>` with ES-DE closed |
   | `shadPS4 version` warns or fails (no version in the Qt launcher) | not a warpzone command: ask the user to open the shadPS4 Qt launcher, Version Manager, download and select a version; failed launches are in `~/.local/state/warpzone/shadps4-launch.log` |
   | Vita3K links dangling or missing | `warpzone links sync <device>` with Vita3K closed |
   | Artwork gamelists stale | `warpzone frontend refresh <device>` with the frontend closed |
   | Unapplied or held-back emulator settings | `warpzone settings audit` (read only) first, then `settings apply` or `settings capture` |
   | Syncthing not running in Game Mode, flatpak access missing | the unit or `flatpak override` command in the `fix:` text |
   | Updater timer, live copy or settings out of step with the config | `warpzone provision device <device>` |
   | `Unattended prune` warns that the half-of-device guard held a removal back | read-only `warpzone prune <device>` to review it; the deletion (`prune --apply`) is the user's call, never a diagnostic step |
   | `Nightly scrape` is `[--]`, or warns that its last check is old or a trouble lasts | schedule `warpzone scrape --unattended` on the hub (see the line's `fix:`), check that the task is enabled, and read its output; a quota streak needs no fix, only a ScreenScraper account for a faster fill |
   | Recent rollback, `stuck`, `dirty` or `dev` checkout, retired pin, no success for 14 days | read `~/.local/state/warpzone/self-update.log` and `~/.local/state/warpzone/updater/last-verify.log`; see [self-update.md](../../../website/src/content/docs/operation/self-update.md#log) |

5. Other read-only commands that help: `warpzone check` (config and missing whitelists), `warpzone status` (folder state, outstanding items, receive-only local changes), `warpzone settings audit`, `warpzone bios`. `warpzone self-update` runs one updater tick, which can change the checkout, so treat it as a change and ask first. Exit code `3` from `provision device --unattended` means deferred (something was open), not failure.
6. After an approved fix, run `warpzone doctor` again and show that the line is now `[ok]`.

## Never

- Delete, reset or clean a checkout, a venv or a Syncthing folder to "fix" something. Do not run `prune --apply`, `inbox --apply` or `settings capture` as a diagnostic step.
- Change a Syncthing folder's path or type by hand. warpzone refuses path changes on purpose ([safety.md](../../../website/src/content/docs/operation/safety.md)).
- Report success without the evidence of a passing command.
