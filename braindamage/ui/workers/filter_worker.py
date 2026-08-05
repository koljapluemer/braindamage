"""Background substring-filter job submitted by SearchFilterBar on every
debounced keystroke. Runs entirely in memory against the dataset already
loaded by a prior DatasetLoadWorker -- no DB access here.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRunnable

from .signals import WorkerSignals


class FilterWorker(QRunnable):
    """`token` is a monotonically increasing generation number the caller uses
    to drop stale results from a keystroke that finished after a later one."""

    def __init__(self, dataset: list, query: str, token: int, filter_fn: Callable[[list, str], list]) -> None:
        super().__init__()
        self._dataset = dataset
        self._query = query
        self._token = token
        self._filter_fn = filter_fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            filtered = self._filter_fn(self._dataset, self._query)
        except Exception as exc:  # surfaced to the user, not swallowed
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit((self._token, filtered))
