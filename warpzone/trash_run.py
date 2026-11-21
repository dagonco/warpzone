"""Deleting a game from the library UI, the server's side: the dry run, the deletion with its
locks and its rollback, the restore, Delete now, and the background task (the whitelist
cleanups and the purge of expired entries). See "Deleting a game" in
development/design/library-ui.md.

The decisions are in `trash.py` (pure) and the disk work in `trash_store.py`; here the two meet
the locks (the hub lock, then the server's write lock) and the hub's Syncthing. See "Concurrency"
in the same page.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from warpzone import (
    catalog_store,
    inbox_lock,
    library_api,
    progress,
    trash,
    trash_record,
    trash_store,
)
from warpzone import whitelist as wl
from warpzone import whitelist_store as store
from warpzone.config import Config
from warpzone.errors import WarpzoneError
from warpzone.inbox_core import InboxError
from warpzone.library_api import ApiError
from warpzone.model import ESDE_ID, LIBRARY_ID, MEDIA_ID
from warpzone.syncthing import Client, SyncthingError

Scan = tuple[str, str]  # (Syncthing folder id, path below its root)
RECORD_SCAN: Scan = (LIBRARY_ID, trash_record.FILE)


class Hub(Protocol):
    """What the server's `HubFiles` gives: the catalog and the whitelists as read, the lock's
    state and the saves of a game."""

    def library(self) -> library_api.Library: ...

    def views(self, cfg: Config) -> dict[str, library_api.DeviceView]: ...

    def invalidate(self) -> None: ...

    def busy(self) -> dict[str, str | None] | None: ...

    def game_saves(self, lib: library_api.Library, game_id: str) -> dict[str, Any]: ...


class Trasher:
    """The operations on the trash. `write_lock` is the server's (not re-entrant): an operation
    that needs it takes it itself and calls nothing that does."""

    def __init__(
        self,
        cfg: Config,
        root: Path,
        hub: Hub,
        write_lock: threading.Lock,
        *,
        client_factory: Callable[[], Client] | None,
        request_run: Callable[[], object] | None,
        log: Callable[[str], None],
        now: Callable[[], datetime],
        arcade: trash_store.ArcadeCache | None = None,
        period: float = trash.CLEANUP_SECONDS,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self.hub = hub
        self.write_lock = write_lock
        self.client_factory = client_factory
        self.request_run = request_run
        self.log = log
        self.now = now
        self.arcade = arcade
        self.period = period
        self._said: set[str] = set()  # what a pass has already logged, until it recovers

    # ------------------------------------------------------------- the pieces

    @contextmanager
    def _hub_lock(self) -> Iterator[None]:
        """The hub lock for the body, or `409 hub-busy` when `inbox/.warpzone.lock` exists."""
        inbox_dir = self.root / "inbox"
        busy = ApiError(
            "hub-busy",
            "an inbox or a scrape is running (inbox/.warpzone.lock exists): wait for it to finish",
        )
        if os.path.lexists(inbox_dir / inbox_lock.LOCK_NAME):
            raise busy
        stack = ExitStack()
        try:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            stack.enter_context(
                inbox_lock.hold(
                    inbox_dir,
                    force=False,
                    now=time.time,
                    out=self.log,
                    mode=inbox_lock.ATTENDED,
                    task=inbox_lock.DELETE,
                )
            )
        except (inbox_lock.LockBusy, inbox_lock.LockStale):
            raise busy from None
        except (OSError, InboxError) as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            raise ApiError("write-failed", f"the hub lock could not be taken ({reason})") from None
        with stack:
            yield

    def _client(self) -> Client | None:
        if self.client_factory is None:
            return None
        try:
            return self.client_factory()
        except WarpzoneError as exc:
            self._once("client", f"warning: no client for the hub's Syncthing ({exc})")
            return None

    def _once(self, key: str, text: str) -> None:
        if key not in self._said:
            self._said.add(key)
            self.log(text)

    def _scan(self, targets: list[Scan], warnings: list[str]) -> None:
        """Ask the hub's Syncthing to scan what changed, without any lock: a failure is a
        warning (its watcher picks the change up anyway, and the cleanup waits for its index)."""
        client = self._client() if targets else None
        if client is None:
            return
        for folder, sub in targets:
            try:
                client.scan(folder, sub=sub)
            except SyncthingError as exc:
                note = f"could not ask the hub to scan {sub}: {exc}"
                warnings.append(note)
                self.log(f"warning: {note}")

    def _ask_run(self) -> None:
        if self.request_run is not None:
            self.request_run()

    def _cleanups(self) -> tuple[list[trash.Cleanup], Any]:
        try:
            return trash_store.read_cleanups(self.root)
        except trash_store.CleanupsReadError as exc:
            raise ApiError("read-failed", str(exc)) from None

    def _write_cleanups(self, items: list[trash.Cleanup], stamp: Any) -> None:
        try:
            trash_store.write_cleanups(self.root, items, stamp)
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            raise ApiError(
                "write-failed", f"the pending whitelist cleanups could not be written ({reason})"
            ) from None

    def _plan(self, game_id: str) -> tuple[trash.DeletePlan, library_api.Library]:
        lib = self.hub.library()
        facts = trash_store.gather(self.root, lib, game_id, arcade=self.arcade)
        return trash.plan_delete(game_id, lib, self.cfg, self.hub.views(self.cfg), facts), lib

    def _answer(self, plan: trash.DeletePlan, lib: library_api.Library) -> dict[str, Any]:
        return trash.plan_response(
            plan,
            saves=self.hub.game_saves(lib, plan.game.id),
            busy=self.hub.busy(),
            purge_after=trash.when_text(trash.purge_time(self.now())),
        )

    # ------------------------------------------------------------- the record

    def _update_record(
        self,
        change: Callable[[list[trash_record.RecordEntry]], list[trash_record.RecordEntry]],
    ) -> bool:
        """Apply `change` to the record of deleted games and write it compacted (see "The record
        of deleted games"). False when nothing changed, so nothing was written. The caller holds
        the write lock. `RecordReadError` or `OSError` when it cannot be read or written."""
        entries, stamp = trash_store.read_record(self.root)
        new = change(entries)
        if new == entries:
            return False
        new = trash_record.compact(
            new,
            self.now(),
            trash_store.paths_at(self.root / "library", trash_record.paths(new)),
        )
        if new == entries or (not new and stamp is None):
            return False
        trash_store.write_record(self.root, new, stamp)
        return True

    def _with_trash_entry(
        self,
        entries: list[trash_record.RecordEntry],
        entry: str,
        manifest: trash.Manifest | None,
    ) -> list[trash_record.RecordEntry]:
        """`entries` with the files under `trash/<entry>/library/` added, unless the entry is
        there already. The deletion time is the manifest's, or the one in the entry's name for
        a damaged entry."""
        if any(e.entry == entry for e in entries):
            return entries
        files = trash_store.trash_files(self.root, entry)
        if files is None:
            return entries
        when = trash_record.parse_when(manifest.deleted) if manifest is not None else None
        when = when or trash_record.name_time(entry) or self.now()
        parts = manifest.game.parts if manifest is not None else ()
        return trash_record.with_entry(
            entries, trash_record.entry_from_trash(entry, when, files, parts)
        )

    def _record_add(self, entry: str, manifest: trash.Manifest | None) -> bool:
        """Record one trash entry (see `_with_trash_entry`)."""
        return self._update_record(lambda es: self._with_trash_entry(es, entry, manifest))

    def _record_failure(self, exc: BaseException) -> str:
        return f"the record of deleted games could not be written ({_reason(exc)})"

    def _record_for_purge(self, entry: str, manifest: trash.Manifest | None) -> bool:
        """Record an entry before it is removed for good. `ApiError` (`write-failed`) when the
        record cannot be written: nothing is removed then."""
        try:
            return self._record_add(entry, manifest)
        except (trash_store.RecordReadError, OSError) as exc:
            raise ApiError(
                "write-failed", f"{self._record_failure(exc)}; nothing was removed", entry=entry
            ) from None

    def _record_deletion(
        self, entry: str, manifest: trash.Manifest, scans: list[Scan], warnings: list[str]
    ) -> None:
        """Add a finished deletion to the record. A failure is a warning: the background pass
        adds the entry."""
        try:
            if self._record_add(entry, manifest):
                scans.append(RECORD_SCAN)
        except (trash_store.RecordReadError, OSError) as exc:
            note = f"{self._record_failure(exc)}; the next background pass will add it"
            warnings.append(note)
            self.log(f"warning: {note}")

    def _unrecord(self, entry: str, scans: list[Scan]) -> trash_record.RecordEntry | None:
        """Take an entry out of the record before its files move back: the entry as it was, or
        None when it was not recorded. `ApiError` (`write-failed`) when the record cannot be
        written; nothing has moved then."""
        taken: list[trash_record.RecordEntry] = []

        def remove(entries: list[trash_record.RecordEntry]) -> list[trash_record.RecordEntry]:
            taken.extend(e for e in entries if e.entry == entry)
            return trash_record.without_entry(entries, entry)

        try:
            if self._update_record(remove):
                scans.append(RECORD_SCAN)
        except (trash_store.RecordReadError, OSError) as exc:
            raise ApiError(
                "write-failed", f"{self._record_failure(exc)}; nothing was restored", entry=entry
            ) from None
        return taken[0] if taken else None

    def _put_back(self, removed: trash_record.RecordEntry | None, scans: list[Scan]) -> None:
        """Record an entry again after a restore gave up (logged when it fails: the background
        pass adds a complete entry that is still in the trash)."""
        if removed is None:
            return
        try:
            if self._update_record(lambda es: trash_record.with_entry(es, removed)):
                scans.append(RECORD_SCAN)
        except (trash_store.RecordReadError, OSError) as exc:
            self.log(f"warning: {self._record_failure(exc)}; {removed.entry} is not in it")

    # ------------------------------------------------------------- the dry run

    def dry_run(self, game_id: str) -> dict[str, Any]:
        plan, lib = self._plan(game_id)
        return self._answer(plan, lib)

    # ------------------------------------------------------------- the deletion

    def delete(self, game_id: str, expect: str | None) -> dict[str, Any]:
        if game_id not in self.hub.library().by_id:
            raise ApiError("not-found", f"no game {game_id!r}", game=game_id)
        if expect is None:
            raise ApiError(
                "expect-missing", "a deletion is applied with the `expect` of its dry run"
            )
        scans: list[Scan] = []
        warnings: list[str] = []
        with self._hub_lock(), self.write_lock:
            answer = self._delete_locked(game_id, expect, scans, warnings)
        self._scan(scans, warnings)
        self._ask_run()
        answer["warnings"] = [*answer["warnings"], *warnings]
        return answer

    def _delete_locked(
        self, game_id: str, expect: str, scans: list[Scan], warnings: list[str]
    ) -> dict[str, Any]:
        self.hub.invalidate()  # plan again from what is on disk now
        plan, lib = self._plan(game_id)  # plan again and compare with `expect`
        try:
            trash.check_expect(plan, expect)
        except ApiError as exc:
            if exc.code != "stale":
                raise
            raise ApiError(
                "stale", exc.message, game=game_id, extra={"plan": self._answer(plan, lib)}
            ) from None
        problem = trash_store.trash_problem(self.root, create=True)
        if problem is not None:
            raise ApiError("trash-unavailable", problem, game=game_id)
        now = self.now()
        entry = trash.entry_name(now, game_id, set(trash_store.entry_names(self.root)))
        # record the whitelist lines in the pending cleanups before anything moves
        cleanups, stamp = self._cleanups()
        cleanup = trash.new_cleanup(plan, entry, now)
        if cleanup is not None:
            self._write_cleanups([*cleanups, cleanup], stamp)
        # create the entry and its manifest, with every path to move
        ours = False  # whether the entry folder was made by this deletion
        try:
            try:
                trash_store.create_entry(self.root, entry)
            except FileExistsError:
                # the name is taken by something that is not ours (a link, a folder made since
                # the name was chosen): it is never touched, only our own recorded lines go
                self._forget(entry)
                raise
            ours = True
            trash_store.write_manifest(self.root, trash.new_manifest(plan, entry, now))
        except OSError as exc:
            if ours:
                self._abandon(entry)
            reason = getattr(exc, "strerror", None) or str(exc)
            raise ApiError(
                "write-failed", f"the trash entry could not be written ({reason})", game=game_id
            ) from None
        # move the parts, then the media
        trash_dir = trash_store.entry_path(self.root, entry)
        try:
            trash_store.transfer(self.root, trash_dir, trash_store.moves_of_plan(plan), strict=True)
        except trash_store.MoveError as exc:
            raise self._rolled_back(game_id, entry, trash_dir, exc) from None
        for item in plan.files:
            scans.append((LIBRARY_ID, item.path))
        scans.extend((MEDIA_ID, m.path) for m in plan.media)
        # remove the gamelist entry
        status, edit = "none", None
        if plan.gamelist_failed:
            status = "failed"
        elif plan.gamelist:
            edit = trash_store.drop_gamelist(self.root, plan.system, plan.gamelist_paths, now)
            status = edit.status
            if edit.warning:
                warnings.append(edit.warning)
        if status == "removed":
            scans.append((ESDE_ID, f"gamelists/{plan.system}/gamelist.xml"))
        # complete the manifest, then update the catalog
        done = trash.new_manifest(
            plan,
            entry,
            now,
            state=trash.COMPLETE,
            blocks=edit.blocks if edit is not None and status == "removed" else (),
            backup=edit.backup if edit is not None and status == "removed" else None,
        )
        try:
            trash_store.write_manifest(self.root, done)
        except OSError as exc:
            note = (
                f"the trash entry could not be marked complete ({_reason(exc)}); "
                "it shows as incomplete"
            )
            warnings.append(note)
            self.log(f"warning: {note}")
        else:
            self._record_deletion(entry, done, scans, warnings)
        try:
            catalog_store.remove_game(self.root, plan.system, game_id, now=now)
        except OSError as exc:
            warnings.append(
                f"the catalog file could not be updated ({_reason(exc)}); the next run will"
            )
        self.hub.invalidate()
        return {
            "dry_run": False,
            "entry": entry,
            "purge_after": done.purge_after,
            "moved": {
                "files": len(plan.files),
                "media": len(plan.media),
                "bytes": plan.size + plan.media_size,
            },
            "gamelist": status,
            "pending": {d: lines.count for d, lines in sorted(plan.whitelists.items())},
            "warnings": list(plan.warnings),
        }

    def _abandon(self, entry: str) -> None:
        """Undo what a deletion did before its first move: its cleanup record and its entry."""
        self._forget(entry)
        trash_store.discard_entry(self.root, entry)

    def _forget(self, entry: str) -> None:
        """Take an entry's pending cleanup out of the file (best effort, logged)."""
        try:
            items, stamp = trash_store.read_cleanups(self.root)
            kept = [c for c in items if c.entry != entry]
            if len(kept) != len(items):
                trash_store.write_cleanups(self.root, kept, stamp)
        except (trash_store.CleanupsReadError, OSError) as exc:
            self.log(f"warning: the pending cleanup of {entry} could not be removed ({exc})")

    def _rolled_back(
        self, game_id: str, entry: str, trash_dir: Path, exc: trash_store.MoveError
    ) -> ApiError:
        """Put back what a failed deletion moved, and the error to answer with: the library as
        it was, or `incomplete` when a move back fails too (the entry and its recorded lines
        stay, and Restore puts back whatever is in the trash)."""
        stuck = trash_store.undo(self.root, trash_dir, exc.done)
        if stuck:
            self.log(f"error: {len(stuck)} path(s) could not be put back from the trash")
            return ApiError(
                "move-failed",
                f"{exc.path} could not be moved to the trash ({exc.why}) and some paths could "
                "not be put back: they are in the Trash view, Restore puts them back",
                game=game_id,
                entry=entry,
                extra={"incomplete": True, "paths": stuck},
            )
        self._abandon(entry)
        if exc.unsafe:
            return ApiError(
                "unsafe-path",
                f"{exc.path}: {exc.why}; nothing was moved",
                game=game_id,
                extra={"paths": [exc.path]},
            )
        return ApiError(
            "move-failed",
            f"{exc.path} could not be moved to the trash ({exc.why}); everything was put back",
            game=game_id,
            extra={"incomplete": False},
        )

    # ------------------------------------------------------------- the listing

    def listing(self, states: Mapping[str, str] | None = None) -> dict[str, Any]:
        """`GET /api/v1/trash`. `states` is the progress state of each device (None when the
        hub did not answer), from which what each device's lines wait for is told."""
        listed = trash_store.list_entries(self.root)
        try:
            cleanups, _ = trash_store.read_cleanups(self.root)
        except trash_store.CleanupsReadError as exc:
            self._once("cleanups", f"warning: {exc}")
            cleanups = []
        views = self.hub.views(self.cfg)
        why: dict[tuple[str, str], str] = {}
        shown = []
        for c in cleanups:
            present = {d: lines for d, lines in c.devices.items() if d in views}
            if not present:
                continue
            shown.append(replace(c, devices=present))
            for device in present:
                why[(c.entry, device)] = trash.waiting_from_state(
                    None if states is None else states.get(device),
                    recorded=c.recorded is not None,
                    managed=trash.is_managed(views[device]),
                )
        return trash.trash_response(listed, trash.pending_rows(shown, why), self.now())

    # ------------------------------------------------------------- restore

    def restore(self, entry: str) -> dict[str, Any]:
        self._require_entry(entry)
        scans: list[Scan] = []
        warnings: list[str] = []
        try:
            with self._hub_lock(), self.write_lock:
                answer = self._restore_locked(entry, scans, warnings)
        finally:
            self._scan(scans, warnings)  # a refusal may have touched the record
        self._ask_run()
        answer["warnings"] = list(warnings)
        return answer

    def _require_entry(self, entry: str) -> None:
        if trash_store.entry_kind(self.root, entry) != trash.F_DIR:
            raise ApiError("not-found", "no such trash entry", entry=entry)

    def _restore_locked(self, entry: str, scans: list[Scan], warnings: list[str]) -> dict[str, Any]:
        if trash_store.entry_kind(self.root, entry) != trash.F_DIR:
            # a purge took the entry between the first look and the locks
            raise ApiError("not-found", "no such trash entry", entry=entry)
        manifest = trash_store.read_manifest(self.root, entry)
        if manifest is None:
            raise ApiError(
                "not-restorable",
                "the entry is damaged and is not restored by itself: Delete now removes it",
                entry=entry,
            )
        trash_dir = trash_store.entry_path(self.root, entry)
        plan = trash.plan_restore(
            manifest,
            trash_store.paths_at(self.root, manifest.paths),
            trash_store.paths_at(trash_dir, manifest.paths),
        )
        if plan.conflicts:
            raise ApiError(
                "restore-conflict",
                f"{len(plan.conflicts)} path(s) exist again where the game was: move or delete "
                "them first",
                entry=entry,
                extra={"paths": list(plan.conflicts)},
            )
        for path in plan.skipped:
            warnings.append(f"{path} is in neither place and was skipped")
        now = self.now()
        unrecorded = self._unrecord(entry, scans)  # before the first move back
        try:
            trash_store.write_manifest(self.root, trash.with_state(manifest, trash.RESTORING))
        except OSError as exc:
            self._put_back(unrecorded, scans)
            raise ApiError(
                "write-failed",
                f"the trash entry could not be written ({_reason(exc)})",
                entry=entry,
            ) from None
        moves = trash_store.moves_of_manifest(manifest, plan.moves)
        try:
            trash_store.transfer(trash_dir, self.root, moves, strict=False)
        except trash_store.MoveError as exc:
            raise self._restore_failed(manifest, trash_dir, exc, unrecorded, scans) from None
        library_moved = [p for p in plan.moves if p.startswith(trash.LIBRARY)]
        media_moved = [p for p in plan.moves if p.startswith(trash.MEDIA)]
        scans.extend((LIBRARY_ID, p.removeprefix(trash.LIBRARY)) for p in library_moved)
        scans.extend((MEDIA_ID, p.removeprefix(trash.MEDIA)) for p in media_moved)
        status = "none"
        if manifest.gamelist is not None and manifest.gamelist.entries:
            edit = trash_store.restore_gamelist(
                self.root, manifest.system, manifest.gamelist.entries, now
            )
            status = edit.status
            if edit.warning:
                warnings.append(edit.warning)
            if status == "restored":
                scans.append((ESDE_ID, f"gamelists/{manifest.system}/gamelist.xml"))
        try:
            found = catalog_store.put_game(self.root, manifest.system, manifest.game, now=now)
            if found == "missing":
                warnings.append("there is no catalog file for the system: the next run adds it")
        except OSError as exc:
            warnings.append(
                f"the catalog file could not be updated ({_reason(exc)}); the next run will"
            )
        kept, removed = self._cancel_cleanups(manifest, warnings)
        if not trash_store.discard_entry(self.root, entry):
            warnings.append("the trash entry still holds other files: Delete now removes it")
        self.hub.invalidate()
        return trash.restore_response(
            manifest, (len(library_moved), len(media_moved)), status, kept, removed, warnings
        )

    def _restore_failed(
        self,
        manifest: trash.Manifest,
        trash_dir: Path,
        exc: trash_store.MoveError,
        removed: trash_record.RecordEntry | None,
        scans: list[Scan],
    ) -> ApiError:
        """Move the paths a failed restore put back into the trash again and record the entry
        again; the error to answer."""
        stuck = trash_store.undo(trash_dir, self.root, exc.done)
        self._put_back(removed, scans)
        if stuck:
            return ApiError(
                "move-failed",
                f"{exc.path} could not be restored ({exc.why}) and some paths could not be "
                "moved back into the trash",
                entry=manifest.entry,
                extra={"incomplete": True, "paths": stuck},
            )
        try:
            trash_store.write_manifest(self.root, manifest)  # the state it had
        except OSError as err:
            self.log(f"warning: the manifest of {manifest.entry} could not be written ({err})")
        if exc.conflict:
            # a file or folder appeared at its place after the plan looked: nothing was replaced
            return ApiError(
                "restore-conflict",
                f"{exc.path} exists again where the game was: move or delete it first; "
                "everything is back in the trash",
                entry=manifest.entry,
                extra={"paths": [exc.path]},
            )
        if exc.unsafe:
            return ApiError(
                "unsafe-path",
                f"{exc.path}: {exc.why}; nothing was restored",
                entry=manifest.entry,
                extra={"paths": [exc.path]},
            )
        return ApiError(
            "move-failed",
            f"{exc.path} could not be restored ({exc.why}); everything is back in the trash",
            entry=manifest.entry,
            extra={"incomplete": False},
        )

    def _cancel_cleanups(
        self, manifest: trash.Manifest, warnings: list[str]
    ) -> tuple[list[str], list[str]]:
        """The restored game's pending cleanups end: its lines are still in the whitelists, so
        those consoles receive it again. Returns the consoles whose lines were still there and
        those whose lines were already removed (the game is off there)."""
        try:
            items, stamp = trash_store.read_cleanups(self.root)
            mine = [c for c in items if c.entry == manifest.entry]
            kept = sorted({d for c in mine for d in c.devices})
            if mine:
                trash_store.write_cleanups(
                    self.root, [c for c in items if c.entry != manifest.entry], stamp
                )
        except (trash_store.CleanupsReadError, OSError) as exc:
            note = f"the pending whitelist cleanups could not be updated ({exc})"
            warnings.append(note)
            self.log(f"warning: {note}")
            return [], []
        removed = sorted(d for d in manifest.whitelists if d not in kept)
        return kept, removed

    # ------------------------------------------------------------- purge

    def purge(self, entry: str) -> dict[str, Any]:
        """Delete now: the whole entry folder, under the write lock only (nothing outside
        `trash/` changes but the record of deleted games, which gets the entry first). It also
        finishes the leftover of a purge that failed partway."""
        scans: list[Scan] = []
        try:
            with self.write_lock:
                manifest = trash_store.read_manifest(self.root, entry)
                if self._record_for_purge(entry, manifest):
                    scans.append(RECORD_SCAN)
                try:
                    size = trash_store.purge_entry(self.root, entry)
                except FileNotFoundError:
                    raise ApiError("not-found", "no such trash entry", entry=entry) from None
                except OSError as exc:
                    raise ApiError(
                        "purge-failed",
                        f"the entry could not be removed whole ({_reason(exc)}); "
                        "what is left stays",
                        entry=entry,
                    ) from None
        finally:
            self._scan(scans, [])
        return {"entry": entry, "purged": True, "bytes": size}

    def purge_pass(self) -> None:
        """Record each complete entry the record lacks, finish what a failed purge left
        (`trash/.purging-*`), then remove each complete entry whose `purge_after` has passed
        (recorded first: it stays when the record cannot be written). Incomplete and damaged
        entries wait for Delete now."""
        scans: list[Scan] = []
        self._record_pass(scans)
        with self.write_lock:
            for name, exc in trash_store.finish_purges(self.root):
                self.log(f"warning: the purge of {name} failed ({_reason(exc)})")
        now = self.now()
        for item in trash_store.list_entries(self.root):
            if not trash.expired(item, now):
                continue
            with self.write_lock:
                fresh = trash.Listed(item.entry, trash_store.read_manifest(self.root, item.entry))
                if not trash.expired(fresh, now):
                    continue
                try:
                    if self._record_add(item.entry, fresh.manifest):
                        scans.append(RECORD_SCAN)
                except (trash_store.RecordReadError, OSError) as exc:
                    self.log(f"warning: {self._record_failure(exc)}; {item.entry} stays")
                    continue
                try:
                    trash_store.purge_entry(self.root, item.entry)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    self.log(f"warning: the purge of {item.entry} failed ({_reason(exc)})")
        self._scan(scans, [])

    def _record_pass(self, scans: list[Scan]) -> None:
        """Add to the record each complete entry of the trash it does not have (the entries a
        hub made before it kept a record, and those whose deletion could not record them)."""
        with self.write_lock:
            try:
                complete = [
                    item
                    for item in trash_store.list_entries(self.root)
                    if item.manifest is not None and item.manifest.state == trash.COMPLETE
                ]
                known = (
                    {e.entry for e in trash_store.read_record(self.root)[0]} if complete else set()
                )
                missing = [item for item in complete if item.entry not in known]
                if not missing:
                    self._said.discard("record")
                    return
                manifests = {item.entry: item.manifest for item in missing}

                def add(entries: list[trash_record.RecordEntry]) -> list[trash_record.RecordEntry]:
                    for entry, manifest in manifests.items():
                        entries = self._with_trash_entry(entries, entry, manifest)
                    return entries

                if self._update_record(add):
                    scans.append(RECORD_SCAN)
            except (trash_store.RecordReadError, OSError) as exc:
                self._once("record", f"warning: {self._record_failure(exc)}")
                return
            self._said.discard("record")

    # ------------------------------------------------------------- the cleanup

    def _reading(
        self, client: Client, connections: Mapping[str, Any], device_id: str
    ) -> progress.Reading:
        try:
            completion: dict[str, Any] | None = client.completion(LIBRARY_ID, device_id)
        except SyncthingError as exc:
            if exc.status != 404:
                raise
            completion = None  # the folder is not shared with the device
        return progress.parse(connections.get(device_id), completion)

    def _remote_need(self, client: Client, device_id: str) -> list[str] | None:
        try:
            return client.remote_need(LIBRARY_ID, device_id)
        except SyncthingError as exc:
            if exc.status != 404:
                raise
            return None

    def cleanup_pass(self) -> list[str]:
        """Remove the whitelist lines of deleted games from the consoles that have received
        the deletion. Returns the devices whose whitelist was written. The hub is asked first
        with no lock held; the writes are made under the write lock (see "Whitelist cleanup")."""
        try:
            items, _ = trash_store.read_cleanups(self.root)
        except trash_store.CleanupsReadError as exc:
            self._once("cleanups", f"warning: {exc}")
            return []
        self._said.discard("cleanups")
        if not items:
            return []
        client = self._client()
        if client is None:
            return []
        valid = {d.name: d for d in library_api.library_devices(self.cfg)}
        recorded: dict[str, str | None] = {}
        readings: dict[str, progress.Reading] = {}
        needs: dict[str, list[str] | None] = {}
        try:
            # Asked on every pass, even of an entry already recorded: a path filed again and
            # deleted again is live in the hub's index until it records the second deletion.
            for c in items:
                answers = {p: client.db_file(LIBRARY_ID, p) for p in c.paths}
                if not trash.in_index(answers):
                    recorded[c.entry] = None
                else:
                    recorded[c.entry] = c.recorded or trash.when_text(self.now())
            connections: Mapping[str, Any] | None = None
            for c in items:
                if recorded[c.entry] is None:
                    continue
                for device in c.devices:
                    if device in valid and device not in readings:
                        if connections is None:
                            connections = client.connections()
                        readings[device] = self._reading(
                            client, connections, valid[device].device_id
                        )
            # A console that has every byte and no deletion left but still "needs" items: ask
            # which, so empty folders it never fetches do not hold its lines for ever.
            for device, reading in readings.items():
                if (
                    reading.need_items > 0
                    and reading.completion >= 100
                    and reading.need_bytes == 0
                    and reading.need_deletes == 0
                ):
                    needs[device] = self._remote_need(client, valid[device].device_id)
        except SyncthingError as exc:
            self._once("hub", f"warning: the whitelist cleanup cannot ask the hub ({exc})")
            return []
        self._said.discard("hub")
        with self.write_lock:
            written = self._apply_cleanups(valid, recorded, readings, needs)
        if written:
            client = self._client()
            for device in written:
                note = store.scan(client, device) if client is not None else None
                if note:
                    self.log(f"warning: {note}")
        return written

    def _apply_cleanups(
        self,
        valid: Mapping[str, Any],
        recorded: Mapping[str, str | None],
        readings: Mapping[str, progress.Reading],
        needs: Mapping[str, list[str] | None],
    ) -> list[str]:
        try:
            items, stamp = trash_store.read_cleanups(self.root)
        except trash_store.CleanupsReadError as exc:
            self._once("cleanups", f"warning: {exc}")
            return []
        lib = self.hub.library()
        views = self.hub.views(self.cfg)
        now = self.now()
        written: list[str] = []
        result: list[trash.Cleanup] = []
        for c in items:
            left = self._settle(c, valid, recorded, readings, needs, views, lib, now, written)
            if left is not None:
                result.append(left)
        if result != items:
            try:
                trash_store.write_cleanups(self.root, result, stamp)
            except OSError as exc:
                # The whitelists are written already: the next pass finds their lines gone.
                self.log(f"warning: the pending cleanups could not be written ({_reason(exc)})")
        return written

    def _settle(
        self,
        c: trash.Cleanup,
        valid: Mapping[str, Any],
        recorded: Mapping[str, str | None],
        readings: Mapping[str, progress.Reading],
        needs: Mapping[str, list[str] | None],
        views: Mapping[str, library_api.DeviceView],
        lib: library_api.Library,
        now: datetime,
        written: list[str],
    ) -> trash.Cleanup | None:
        """One cleanup after this pass: what it still waits for, or None when nothing is left
        (a console that left, a path that exists again, a console whose lines can go)."""
        left = trash.only_devices(c, set(valid))
        if left is None:
            return None
        left = trash.reappeared(left, trash_store.paths_at(self.root / "library", left.paths))
        if left is None:
            return None
        if left.entry in recorded and left.recorded != recorded[left.entry]:
            left = replace(left, recorded=recorded[left.entry])
        for device in list(left.devices):
            view = views[device]
            names = needs.get(device)
            why = trash.waiting(
                recorded=left.recorded is not None,
                managed=trash.is_managed(view),
                reading=readings.get(device),
                needed=None if names is None else trash.needs_any(names, left.paths),
            )
            if why is not None:
                continue
            outcome = self._clean_device(device, view, left, lib, now)
            if outcome == "written":
                written.append(device)
            if outcome in ("written", "gone"):
                after = trash.without_device(left, device)
                if after is None:
                    return None
                left = after
        return left

    def _clean_device(
        self,
        device: str,
        view: library_api.DeviceView,
        c: trash.Cleanup,
        lib: library_api.Library,
        now: datetime,
    ) -> str:
        """Remove one device's recorded lines: `written`, `gone` (they are not in the file any
        more), or `kept` (the effect check, or a change in between, keeps them for later)."""
        model = view.model
        if model is None:
            return "kept"
        new = trash.cleaned_model(model, c, device)
        if new == model:
            return "gone"
        if not trash.effect_holds(model, new, lib):
            self.log(
                f"warning: removing the lines of {c.title} from {device} would change another "
                "game; they stay"
            )
            return "kept"
        data = wl.render(new).encode("utf-8")
        try:
            store.replace(
                self.root,
                device,
                library_api.raw_etag(view.etag),
                lambda _snapshot: data,
                now=now,
            )
        except store.Stale:
            return "kept"  # read again on the next pass
        except (store.NoLibrary, OSError, WarpzoneError) as exc:
            self.log(f"warning: the whitelist of {device} could not be written ({_reason(exc)})")
            return "kept"
        return "written"

    # ------------------------------------------------------------- the background task

    def has_work(self) -> bool:
        return (
            os.path.lexists(trash_store.cleanups_path(self.root))
            or bool(trash_store.entry_names(self.root))
            or bool(trash_store.purging_names(self.root))
        )

    def tick(self) -> None:
        """One pass of the background task: the cleanup, then the purge. A failure of one never
        stops the other or the thread."""
        for name, job in (("cleanup", self.cleanup_pass), ("purge", self.purge_pass)):
            try:
                job()
            except Exception as exc:  # a background pass must never take the server down
                self.log(f"error: the trash {name} failed ({type(exc).__name__})")

    def loop(self, stop: threading.Event) -> None:
        """At start and then every `period` seconds while the cleanups file or the trash has
        entries."""
        while not stop.is_set():
            if self.has_work():
                self.tick()
            if stop.wait(self.period):
                return


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
