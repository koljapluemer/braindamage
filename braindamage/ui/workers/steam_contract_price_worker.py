"""Background job for the contract detail dialog's "Fetch prices from Steam
and recalculate" button -- wraps steam_market_api.refresh_contract_prices off
the UI thread, since it's rate-limited to one request every couple of seconds
and a contract can reference a couple dozen skins between its inputs and
possible outputs.
"""

from __future__ import annotations

from PySide6.QtCore import QRunnable

from ... import steam_market_api
from ...db import SessionLocal
from ...models import Contract
from .signals import WorkerSignals


class SteamContractPriceWorker(QRunnable):
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
                result = steam_market_api.refresh_contract_prices(session, contract, on_progress=on_progress)
        except Exception as exc:  # surfaced to the user, not swallowed
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(result)
