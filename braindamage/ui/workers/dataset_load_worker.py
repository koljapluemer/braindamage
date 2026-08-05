"""Loads a page's full dataset (e.g. every Skin row) once per page visit, off
the UI thread -- the counterpart to FilterWorker, which then filters that
already-in-memory dataset on every debounced keystroke without touching the DB
again.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRunnable

from .signals import WorkerSignals


class DatasetLoadWorker(QRunnable):
    def __init__(self, loader: Callable[[], list]) -> None:
        super().__init__()
        self._loader = loader
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            rows = self._loader()
        except Exception as exc:  # surfaced to the user, not swallowed
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(rows)
