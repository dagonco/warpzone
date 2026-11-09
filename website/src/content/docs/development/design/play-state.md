---
title: "Play state"
---

Favourites and play statistics of every frontend reach the others. It is opt-in per device (the
`play-state` folder group) and data-driven: a profile gives the folder (`paths.play_state`) and
a gamelist to work on (`paths.esde`, or `[artwork]` with a format), so a new frontend is one more
`artwork.Format`, which names its three tags in `state_tags`. Nothing tests a device or profile
name, nothing needs SSH, and the hub runs no code: it only stores the files, like any folder.

**Transport.** One two-way folder, `wz-play-state` (`frontend/play-state` on the hub, with
history), holds one JSON Lines file per device, `<device>.jsonl`, written by that device only:

```
{"v":1,"device":"rg351mp","replica":"rg351mp"}
{"system":"gba","path":"Hacks/Beta.gba","plays":3,"included":2,"last":1793000000,"fav":false,"fav_at":1793000100,"playtime":5400}
```

Lines are keyed by hub system and ROM path below it, so a frontend's own system names
(`pokemonmini`, `ppsspp`) never reach the file, and sorted, so the same state gives the same
bytes and Syncthing sends nothing. The alternative was sending the Anbernic's data through
`wz-esde`, which would have meant changing that folder's type and ignores on the devices, and a
single shared file would conflict in Syncthing as soon as two devices wrote it. With one writer
per file there are no conflicts, and `wz-esde` and `wz-media` stay exactly as they are (a test
compares their hub and device specs with and without the group). The folder carries one more
file per device, `<device>.status.json`, the disk report of the library UI (see
`development/design/library-ui.md`, "Disk space"); the ledger readers glob `*.jsonl`, so it is
invisible to them and to the merge. A device whose file cannot be
read is left out of the merge with a warning; a device whose own file cannot be read changes
nothing. Conflict copies are ignored. Files of devices that left the config still count: their
plays happened.

**Compatibility.** `playtime` is an optional field of a record and the version stays `1`: a
reader takes only the keys it knows, so an older warpzone ignores `playtime` and a ledger
written before it reads as zero. Nothing is migrated, and no device has to update before
another. A device publishes playtime from its next run on its new version; until then its
ledger simply lacks it. A version number would have made every older device refuse the ledgers
of the newer ones, which is a worse failure than a missing statistic.

**Replicas.** The trap is that Deck and Machine share one gamelist. A count that rises in the
shared file may be a play on this device or one Syncthing delivered, and each device claiming
the difference as its own would count every play twice. So the unit of counting is the
*replica*, a gamelist: every device with `paths.esde` is in the replica `wz-esde`, any other
device is a replica of its own. A device's `plays` is its view of its replica's counter. The
highest view counts within a replica and replicas add up, which is the requested sum across
devices once the devices that read the same file are taken as one. Plays made on Deck and
Machine at the same time while offline are still decided by `wz-esde`'s last-write-wins, as they
always were.

**Merge rules** (`playstate.plan` and `merge`, pure; a merge depends only on the set of ledgers):

- *Play count.* A grow-only counter per replica; shown value = own + other replicas. The shown
  value also holds other replicas' plays, so a device must subtract them to read its own:
  `plays = max(plays, shown - included)`. The first run has `included = 0`, so existing counts
  are absorbed once, by the replica that has them, and `max` makes an older gamelist harmless.
  `included` has to arrive together with the count it explains. If it lived only in the ledger,
  the shared gamelist could arrive first (they are separate Syncthing folders) and the merged
  plays would read as local ones. So a shared gamelist carries it, in a `<warpzonePlayState>`
  child of `<gameList>` listing only games with plays from elsewhere. ES-DE's `updateGamelist`
  reloads the file and replaces only changed `<game>` nodes, keeping unknown children, and
  reads only `game` and `folder`. A generated gamelist belongs to one device, which keeps
  `included` in its own ledger, because such a frontend may rewrite the whole file. If a future
  ES-DE dropped the element, the ledger takes over; two devices running before their ledgers
  cross could then overcount once, and the count would not keep growing (tested).
- *Favourite.* A last-writer-wins register per game with a change time. A value that differs
  from the device's record is a change, so an unset is recorded rather than being a missing
  value, and a device that still shows the old value receives the unset instead of bringing the
  favourite back. Equal times resolve to `true`, the same on every device. A favourite that a
  device already had at its first run is stamped `1`, older than any real change: first runs
  join their favourites and undo no recorded unset.
- *Last played.* The latest instant wins.
- *Playtime.* The seconds ES-DE records in `<playtime>` of its gamelist, published as `playtime`.
  It is a grow-only value per replica, like the count: `max(playtime, gamelist value)`, so an
  older gamelist takes nothing back. It is **not** merged into other replicas' gamelists: a
  frontend that records no playtime (the RetroPie lineage's EmulationStation, so the Anbernic)
  has nothing to receive it in, and ES-DE adds to its own value as the player plays, which
  would race a value written from outside. Readers add it up instead: the library UI's API
  sums the playtime of a game across replicas (the highest value within a replica, because
  Deck and Machine read the same gamelist), and shows each device's own value beside the total.
  A frontend whose `state_tags` name no `playtime` tag publishes none, and the field is absent
  from its records (`artwork.ESDE_STATE_TAGS` has it, `STATE_TAGS` does not).
- `hidden` and `kidgame` stay per device. Scraped fields and media are not touched.

**Clocks.** Gamelists hold naive local time, and consoles differ (one reported EST while the
others use CET, with the same correct instant). Ledgers hold UTC epochs: a device converts with
its own zone when it reads a gamelist and when it writes one. A favourite's change time is the
gamelist's modification time, capped at the device's now: closer to the real change than the
moment of the run, which may be days later, and free of zones. Clocks can also be wrong, so a
change is always stamped after anything the device has seen for that game (a Lamport clock). A
change made after seeing another therefore wins on a slow clock, and a device whose clock ran
ahead can still be overridden once seen. Only changes that never saw each other are decided by
the clocks themselves. A last played time more than a day ahead of the reader is ignored as
a wrong clock, and stays in its ledger.

**Absent games.** A game that is neither in the gamelist nor on the device keeps its record
and gets nothing (`present`), so state waits for a ROM that may arrive and survives a ROM that
left. A game in the gamelist is updated even when its ROM is elsewhere (a shared file lists
what other devices have). A ROM that is here but missing from the gamelist gets an entry; ES-DE
fills in the name.

**ES-DE.** `gamelists/<system>/gamelist.xml` is edited only while ES-DE has no gamelist in
memory. ES-DE fires its `startup` event before `loadSystemConfigFile()` reads them and waits for
the script, so `<esde>/scripts/startup/warpzone-play-state.sh` runs `play-state sync
--frontend-starting`, which is the one case where the process is alive and the files are still
free. The flag makes `running` skip the processes above this one, so another ES-DE still counts.
With ES-DE open a run only reads and publishes. A `[play_state] refresh` timer
(`warpzone-play-state.timer`) publishes between starts, and merges when ES-DE is closed.
Edits (`edit_gamelist`, pure) change only the three tags and the element, keep every other byte
and line ending, must leave every other field of every entry equal, and never remove an
entry. Each file is backed up to `<state>/warpzone/play-state/backups` and written atomically,
refused if it changed. A run is bounded by the script's `timeout`, imports no network code, and
took about 30 ms on 767 games. Gamelists that say the same are not written.

**Generated gamelists.** `ensure_gamelists` reads the frontend's files and the ledgers
(`prepare_generated`) and hands the result to `artwork.build` as an overlay of user fields, so
`frontend refresh` and its timer do all the work, on the same "only while closed" rule. With
the frontend open the device publishes what it saved last and writes no gamelist. The ledger is
written before the gamelists: a run cut short between the two loses at worst the plays of that
moment, where the other order would count every merged play as played here.

**Known limits.** Deck and Machine still overwrite each other's gamelist when both change it
offline, so a favourite applied on one can look like an unset on the other if an older file
wins. Counts only grow, so resetting one in ES-DE comes back at the next merge. Latency is one
timer period plus Syncthing, or the next ES-DE start for what reaches a running ES-DE.
