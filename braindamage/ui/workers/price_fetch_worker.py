"""Background price-fetch job for the Maintenance page's "Fetch prices for
selected" action -- wraps cs2cap_api.run_price_import off the UI thread. Direct
port of the Textual version's `@work(thread=True, exclusive=True)` worker
(braindamage/screens/maintenance.py).
"""

from __future__ import annotations

from PySide6.QtCore import QRunnable

from ... import cs2cap_api
from ...db import SessionLocal
from ...models import Skin
from .signals import WorkerSignals


class PriceFetchWorker(QRunnable):
    def __init__(self, skin_id: str) -> None:
        super().__init__()
        self._skin_id = skin_id
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            with SessionLocal() as session:
                skin = session.get(Skin, self._skin_id)
                if skin is None:
                    self.signals.error.emit("Skin not found.")
                    return
                skin_name = skin.name
                result = cs2cap_api.run_price_import(session, skin)
        except Exception as exc:  # surfaced to the user, not swallowed
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit((skin_name, result))
