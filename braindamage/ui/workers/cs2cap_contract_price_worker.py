"""Background job for the contract detail dialog's "Fetch prices from CS2Cap
and recalculate" button -- wraps cs2cap_api.refresh_contract_prices off the
UI thread, the same CS2Cap API the Maintenance page's "Fetch prices for
selected" button already uses, run across every skin a contract references
instead of just one.
"""

from __future__ import annotations

from PySide6.QtCore import QRunnable

from ... import cs2cap_api
from ...db import SessionLocal
from ...models import Contract
from .signals import WorkerSignals


class Cs2capContractPriceWorker(QRunnable):
    def __init__(self, contract_id: str) -> None:
        super().__init__()
        self._contract_id = contract_id
        self.signals = WorkerSignals()

    def run(self) -> None:
        def on_progress(done: int, total: int) -> None:
            self.signals.progress.emit(done, total)

        try:
            with SessionLocal() as session:
                contract = session.get(Contract, self._contract_id)
                if contract is None:
                    self.signals.error.emit("Contract not found.")
                    return
                result = cs2cap_api.refresh_contract_prices(session, contract, on_progress=on_progress)
        except Exception as exc:  # surfaced to the user, not swallowed
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(result)
