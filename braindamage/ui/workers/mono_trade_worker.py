"""Background mono-trade generation job for the Maintenance page's
"(Re)generate mono trades..." button -- wraps mono_trades.generate_mono_trades
off the UI thread, since it scans essentially the whole catalog.
"""

from __future__ import annotations

from PySide6.QtCore import QRunnable

from ... import mono_trades
from ...db import SessionLocal
from .signals import WorkerSignals


class MonoTradeWorker(QRunnable):
    def __init__(self, max_input_cost: float) -> None:
        super().__init__()
        self._max_input_cost = max_input_cost
        self.signals = WorkerSignals()

    def run(self) -> None:
        def on_progress(done: int, total: int) -> None:
            self.signals.progress.emit(done, total)

        try:
            with SessionLocal() as session:
                rows = mono_trades.generate_mono_trades(
                    session, max_input_cost=self._max_input_cost, on_progress=on_progress,
                )
        except Exception as exc:  # surfaced to the user, not swallowed
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(len(rows))
