"""Background job for the Maintenance page's "Refetch all skin prices" action --
wraps cs2cap_api.run_bulk_price_import off the UI thread. Only reachable when
config.CS2CAP_PREMIUM_TIER is set (checked by the page before starting this).
"""

from __future__ import annotations

from PySide6.QtCore import QRunnable

from ... import cs2cap_api
from ...db import SessionLocal
from .signals import WorkerSignals


class BulkPriceFetchWorker(QRunnable):
    def __init__(self) -> None:
        super().__init__()
        self.signals = WorkerSignals()

    def run(self) -> None:
        def on_progress(done: int, total: int) -> None:
            self.signals.progress.emit(done, total)

        try:
            with SessionLocal() as session:
                result = cs2cap_api.run_bulk_price_import(session, on_progress=on_progress)
        except Exception as exc:  # surfaced to the user, not swallowed
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(result)
