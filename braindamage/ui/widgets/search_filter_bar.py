"""Debounced, off-main-thread search-and-filter component.

Wraps a QLineEdit. The full dataset (e.g. every Skin row) is loaded once via
`load()`, not re-queried per keystroke. Each keystroke restarts a short debounce
timer; on timeout the actual substring filtering runs on a QThreadPool worker,
tagged with a monotonically increasing generation token so a slow filter from
an earlier keystroke can never overwrite a newer one's results.

This is the single implementation of the "search Input -> filter -> rebuild
table" pattern that was previously duplicated (and fully synchronous, on the
main thread, with no debounce) across the Maintenance/Skins screens and the
skin-picker modal.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from ..workers.dataset_load_worker import DatasetLoadWorker
from ..workers.filter_worker import FilterWorker
from ..workers.signals import keep_alive

DEBOUNCE_MS = 200


class SearchFilterBar(QWidget):
    resultsReady = Signal(list)
    datasetLoaded = Signal(list)
    loadFailed = Signal(str)

    def __init__(
        self, filter_fn: Callable[[list, str], list], placeholder: str = "Filter...", parent=None
    ) -> None:
        super().__init__(parent)
        self._filter_fn = filter_fn
        self._dataset: list = []
        self._generation = 0
        self._inflight: list = []

        self._input = QLineEdit(self)
        self._input.setPlaceholderText(placeholder)
        self._input.textChanged.connect(self._on_text_changed)

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._run_filter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._input)

    @property
    def line_edit(self) -> QLineEdit:
        return self._input

    def focus(self) -> None:
        self._input.setFocus()

    def load(self, loader: Callable[[], list]) -> None:
        """Kicks off a background dataset load (e.g. a fresh `select(Skin)`
        against a new SessionLocal). On completion the dataset is filtered
        against whatever's currently typed and results are (re-)emitted."""
        worker = DatasetLoadWorker(loader)
        keep_alive(self._inflight, worker)
        worker.signals.finished.connect(self._on_dataset_loaded)
        worker.signals.error.connect(self.loadFailed.emit)
        QThreadPool.globalInstance().start(worker)

    def set_dataset(self, rows: list) -> None:
        """Sets the in-memory dataset directly, skipping a load worker -- for
        callers that already have the full list in hand (e.g. the skin picker
        dialog's one-time `eligible_input_options()`), while still getting
        debounced, off-thread filtering on every keystroke."""
        self._on_dataset_loaded(rows)

    def _on_dataset_loaded(self, rows: list) -> None:
        self._dataset = rows
        self.datasetLoaded.emit(rows)
        self._run_filter()

    def _on_text_changed(self, _text: str) -> None:
        self._debounce_timer.start(DEBOUNCE_MS)

    def _run_filter(self) -> None:
        self._generation += 1
        token = self._generation
        query = self._input.text()
        worker = FilterWorker(self._dataset, query, token, self._filter_fn)
        keep_alive(self._inflight, worker)
        worker.signals.finished.connect(self._on_filter_finished)
        worker.signals.error.connect(self.loadFailed.emit)
        QThreadPool.globalInstance().start(worker)

    def _on_filter_finished(self, payload: tuple[int, list]) -> None:
        token, filtered = payload
        if token != self._generation:
            return  # a newer keystroke already superseded this request
        self.resultsReady.emit(filtered)
