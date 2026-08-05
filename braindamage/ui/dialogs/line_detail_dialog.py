"""Float value + quantity entry for one contract line -- port of the Textual
LineDetailModal, now using FloatWearPreview for a live wear/price preview that
the original Textual port dropped (the old Streamlit page had it).
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QSpinBox, QVBoxLayout

from ...tradeup import SkinOption
from ..widgets.float_wear_preview import FloatWearPreview


class LineDetailDialog(QDialog):
    def __init__(
        self,
        option: SkinOption,
        max_quantity: int,
        initial_float: float | None = None,
        initial_quantity: int = 1,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(option.label)
        self._option = option
        self._max_quantity = max_quantity
        self._result: tuple[float, int] | None = None

        initial_float = initial_float if initial_float is not None else (option.min_float + option.max_float) / 2
        initial_quantity = max(1, min(initial_quantity, max_quantity))

        self._float_preview = FloatWearPreview(option.skin_id, option.min_float, option.max_float, self)
        self._float_preview.set_value(initial_float)

        self._qty_spin = QSpinBox(self)
        self._qty_spin.setRange(1, max_quantity)
        self._qty_spin.setValue(initial_quantity)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: #dc2626;")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(option.label))
        layout.addWidget(QLabel(f"Float range: {option.min_float:.4f} – {option.max_float:.4f}"))
        layout.addWidget(self._float_preview)
        layout.addWidget(QLabel(f"Quantity (max {max_quantity})"))
        layout.addWidget(self._qty_spin)
        layout.addWidget(self._error_label)
        layout.addWidget(buttons)

    def _confirm(self) -> None:
        float_value = self._float_preview.value()
        quantity = self._qty_spin.value()
        if not (self._option.min_float <= float_value <= self._option.max_float):
            self._error_label.setText(
                f"Float must be within {self._option.min_float:.4f}-{self._option.max_float:.4f}."
            )
            return
        if not (1 <= quantity <= self._max_quantity):
            self._error_label.setText(f"Quantity must be between 1 and {self._max_quantity}.")
            return
        self._result = (float_value, quantity)
        self.accept()

    def result_value(self) -> tuple[float, int] | None:
        return self._result
