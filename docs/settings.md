# Emulator settings

How to keep the same language, region and graphics choices on every console, without copying whole config files.

You change a setting in an emulator's own interface, on whichever console is at hand. `warpzone settings capture` takes it to the hub. Every other console applies it with `warpzone settings apply`, which ES-DE runs at startup. The hub holds **layers of single keys**, never whole config files: those also hold controller mappings, GPU ids, window sizes, absolute paths and logins, which differ from one console to the next.

## The layers

Layers live in the `settings` folder of the hub (folder `wz-settings`, which a device joins with the `settings` group):

```
settings/
├── common.toml                 keys every device gets
├── <device>.toml               keys of one device; a key here wins over common.toml
└── files/<emulator>/<id>       system data, whole files, the same on every device
```

```toml
# settings/common.toml
format = 1

[eden.qt-config]
"[System]language_index" = "1"

[retroarch.cfg]
savestate_auto_save = "true"
```

Each table is `[<emulator>.<file>]`, and each entry is a key of that file with its value. The device layer wins over common, and a device cannot unset a common key, only give it another value. Removing a key from the layers stops managing it: the console keeps the value last written. `capture` writes these files for you; you can also edit them on the hub. [design.md](design.md#emulator-settings) has the full format.

The folder is two-way (unlike `tools`, which is receive-only on devices), because captures are written on a console. The hub keeps 30 days of versions of every layer, which is the history of each change.

Eleven emulators are covered by the `steamos-emudeck` profile: Eden, RetroArch (and its core options), DuckStation, PCSX2, Dolphin, Azahar, melonDS, PPSSPP, Vita3K, xemu and Ryujinx.

## Set up

1. Add `"settings"` to `folders` for each device in the config (see [`examples/config.example.toml`](../examples/config.example.toml)).
2. On the hub: `warpzone provision hub`. On each console: `warpzone provision device <name>`.
3. Wait until Syncthing reports `wz-settings` idle and in sync on the console. Capture refuses to run before that.

`provision device` also installs the ES-DE startup script (`~/ES-DE/scripts/startup/warpzone-settings.sh`), which runs `settings apply` each time ES-DE starts and never blocks it for more than 15 seconds. ES-DE only runs scripts when *Enable custom event scripts* is on (Main menu > Other settings). `provision` turns it on in `es_settings.xml`, but only while ES-DE is closed, because ES-DE rewrites that file when it quits. If ES-DE is running, `provision` warns and leaves the file alone: quit ES-DE and run `provision device` again, or turn the option on in ES-DE yourself. `warpzone doctor` reminds you while it is off.

The script uses the Python that ran `provision`. Install warpzone (for example with pipx) before provisioning; from a clone `provision` warns and installs nothing.

## First capture, on the reference console

Pick the console that is already configured the way you like and run, on it:

```bash
warpzone settings capture --seed --dry-run    # preview: asks or decides, prints, writes nothing
warpzone settings capture --seed              # the real run
```

`--seed` proposes one entry for every key the profile marks as `known` (language, region, user name and other settings meant to be shared). Add `--key` to propose more, with patterns of the form `<emulator>/<file>/<key>`:

```bash
warpzone settings capture --seed --key 'eden/qt-config/*'
```

System data files (the xemu EEPROM, Azahar's `sysdata` config, the Vita3K user file) are not `known` keys, so `--seed` alone never proposes them; name them with `--key`. The first capture on the reference console, for the settings and all three data files, is:

```bash
warpzone settings capture --seed --key 'xemu/data/*' 'azahar/data/*' 'vita3k/data/*'
```

Run it before ES-DE has started and applied on that console: a first `settings apply` takes a data file that has no hub copy as its baseline, and a plain `capture` then proposes it only after it changes. If that already happened, the same `--seed --key` command still proposes it.

On a terminal you answer one prompt per key:

```
eden  qt-config  [Renderer]resolution_setup
  here 3   before 2   common -   deck 2
  [c]ommon  [D]evice (deck)  [x] discard  [s]kip  [q]uit >
```

The capital letter is the pre-selection and Enter takes it. Keys that are about this console (resolution, docked mode, device name) go to the device layer by default; everything else goes to common. `q` skips what is left. Without a terminal, use `--yes` to take every pre-selection, or decide with `--to-common`, `--to-device` and `--discard` patterns:

```bash
warpzone settings capture --seed --yes
warpzone settings capture --to-common 'eden/qt-config/[System]*' --discard 'retroarch/*'
```

With no decision flag and no terminal the run is a report and writes nothing.

Capture runs on the console, needs `/proc` (Linux; it refuses on macOS) and a local Syncthing that reports `wz-settings` idle and in sync, so it never builds on layers that are about to change. `--offline` skips the Syncthing check; use it only if you know the folder is up to date. It also skips an emulator that is running and refuses while a layer has a conflict copy (`*.sync-conflict-*`): keep one version on the hub and delete the other.

Then run `warpzone settings apply` on the other consoles, or just start ES-DE on them.

## Every day

Change a setting in the emulator, close the emulator, and run:

```bash
warpzone settings audit      # what differs between the layers and this console
warpzone settings capture    # choose what goes to the hub
```

Capture proposes keys that changed since the last sync. Keys that an emulator update added to its file are only counted; `--include-new` lists them. Each key can go three ways:

| Choice | Effect |
|---|---|
| common | Set in `common.toml` and removed from this device's layer, where it would keep winning. |
| device | Set in `<device>.toml`; only this console gets it. |
| discard | Nothing reaches the hub; the layer value comes back at the next apply. |

Skipping a key leaves it for next time. System data files (for example the Xbox EEPROM) are shown by size and hash only, and can go to common, be discarded or be skipped. A data file that has no baseline yet is proposed only by `capture --seed --key '<emulator>/data/<id>'`, for example `'xemu/data/*'`.

## Apply

```bash
warpzone settings apply --dry-run    # print the plan, write nothing
warpzone settings apply
```

Apply works offline from the local copy of the folder and never calls Syncthing. For every layered key it writes the layer value only if the console has not changed the key since the last sync. The rules that make it safe:

- **Held keys.** A key you changed in the emulator and have not captured yet is held: apply does not overwrite it and says so. Capture it (or discard it), or run `warpzone settings apply --overwrite-local` to write the layer value anyway.
- **All or nothing.** A layer that fails validation (it does not parse, the `format` is missing, a value has the wrong kind) means apply touches nothing, since common without the device layer would put another console's choices in place.
- **Never while the emulator runs.** Apply checks before it reads and again right before it writes. A running emulator is skipped and logged, which is not a failure.
- **Only layered keys change.** Every other byte of a file stays as it was, and each write is atomic and comes after a backup.
- **Logins never reach the hub.** Usernames, tokens and passwords are never captured, applied, logged or shown; a secret key found in a layer is reported by name only.

Use `--emulator ID ...` to limit a run and `--device NAME` when the console cannot be inferred from the local Syncthing. Exit code `0` means every file was handled (held keys and running emulators are not failures), `1` an invalid layer or a file that could not be handled, `2` a usage or config error.

## Audit

`warpzone settings audit` reads the layers, the files and the state, and writes nothing:

```
eden  qt-config  ~/.config/eden/qt-config.ini
  in sync        14 keys
  pending        [Renderer]resolution_setup  here 3, hub 2      apply writes it
  changed here   [System]language_index      here 4, hub 1      capture proposes it; apply holds it
```

`--all` also lists the keys that are in sync. Values are cut to 40 characters and secrets never appear. It exits `0` when everything is in sync and `1` otherwise, so it can run in a script.

## Where the state is

On each console, outside every synced folder, in `~/.local/state/warpzone/settings/` (`$XDG_STATE_HOME` is honoured):

- `backups/<emulator>/<file>/`: the file as it was before each write, the last 10 per file. Backups are whole files and may hold the emulator's login tokens, so they never leave the console.
- `log`: one line per run and per change (emulator, file and key name, never a value), rotated at 1 MiB.
- `snapshots/`: the value each key had when it was last synchronised. This is how warpzone tells a change made in an emulator from one that came from the hub.
- `last-apply.json`: read by `warpzone doctor`.

To undo a bad apply, copy the file back from `backups/`, with the emulator closed. To undo a bad capture, restore the older version of the layer from the hub's file versions.

## The Xbox EEPROM caveat

Some settings live outside the config files, so the profile keeps a few whole files on the hub, in `settings/files/<emulator>/<id>`, and `apply` delivers them like keys. The xemu EEPROM is one of them: it holds the language and video mode, but also the serial number, MAC address and hard disk key. Sharing it gives every console the same Xbox identity, with the same serial and signing key.

xemu's hard disk image stays per console and is not synced. A console that already had its own EEPROM may therefore refuse saves signed with the old key that sit inside its own xemu HDD image, after the first apply. If you care about those saves, leave the EEPROM out: do not choose it at capture, or discard it. See also [emudeck.md](emudeck.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `capture` refuses: not idle or not in sync | Syncthing is still moving layers. Wait for `wz-settings` to settle, then run it again. |
| `capture` names a conflict copy | Two consoles wrote the same layer. On the hub keep the version you want and delete the other `*.sync-conflict-*` file. |
| A key stays `changed here` after apply | It is held. Capture it, discard it, or use `apply --overwrite-local`. |
| Apply did nothing and printed nothing | Check `~/.local/state/warpzone/settings/log`: a running emulator, a missing file (the emulator never ran) or an invalid layer shows up there. |
| Apply says a key cannot be written | The key appears twice in the file, or the handler cannot add it. Set it once in the emulator's interface. |
| The startup script does not run | `warpzone doctor` shows the script, `CustomEventScripts` and the last apply. Fix with `warpzone provision device <name>` while ES-DE is closed. |
| `capture` or `apply` refuses on macOS | Both need `/proc` to detect running emulators, so they run on Linux consoles only. |

`warpzone doctor` checks the layers, the startup script and the last apply ([doctor.md](doctor.md)).
