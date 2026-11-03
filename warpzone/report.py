"""What a command tells its caller: the per-folder notes, the warnings, the deferred flag."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Reporter:
    out: Callable[[str], None] = print
    warnings: list[str] = field(default_factory=list)
    warn_sink: Callable[[str], None] | None = None
    # A write was held back because the menu (or an emulator) is open: an unattended run
    # reports it to its caller as "try again later" instead of "done".
    deferred: bool = False
    _notes: dict[str, list[str]] = field(default_factory=dict)

    def note(self, folder_id: str, text: str) -> None:
        """Collect what happened to one folder; `flush` prints it as a single line."""
        self._notes.setdefault(folder_id, []).append(text)

    def flush(self, folder_id: str) -> None:
        notes = self._notes.pop(folder_id, [])
        self.out(f"{folder_id}: {', '.join(notes) if notes else 'unchanged'}")

    def defer(self) -> None:
        self.deferred = True

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        if self.warn_sink:
            self.warn_sink(f"warning: {message}")
