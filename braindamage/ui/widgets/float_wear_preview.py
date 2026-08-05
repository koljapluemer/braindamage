"""Composite float-entry control used by LineDetailDialog: a synced spin box +
slider for a trade-up input's float value, with a live wear-bucket + known-
price preview below. Restores the live preview the old Streamlit page had
that the first (Textual) port dropped -- the current price/wear lookup here is
a cheap single-skin signal-file read, so it runs directly on the UI thread
with just a light debounce rather than a full QThreadPool round trip.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

from ...pricing import latest_price_for_wear
from ...tradeup import wear_for_float

_PREVIEW_DEBOUNCE_MS = 100
_SLIDER_STEPS = 10_000


class FloatWearPreview(QWidget):
    """Emits `valueChanged(float)` whenever the confirmed float value changes,
    already clamped to [min_float, max_float]."""

    valueChanged = Signal(float)

    def __init__(self, skin_id: str, min_float: float, max_float: float, parent=None) -> None:
        super().__init__(parent)
        self._skin_id = skin_id
        self._min_float = min_float
        self._max_float = max_float
        self._syncing = False

        self._spin = QDoubleSpinBox(self)
        self._spin.setDecimals(4)
        self._spin.setRange(min_float, max_float)
        self._spin.setSingleStep(0.0001)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(0, _SLIDER_STEPS)

        self._wear_label = QLabel(self)
        self._price_label = QLabel(self)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._update_preview)

        self._spin.valueChanged.connect(self._on_spin_changed)
        self._slider.valueChanged.connect(self._on_slider_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._spin)
        layout.addWidget(self._slider)
        info = QHBoxLayout()
        info.addWidget(self._wear_label)
        info.addWidget(self._price_label)
        layout.addLayout(info)

    def set_value(self, value: float) -> None:
        value = min(max(value, self._min_float), self._max_float)
        self._spin.setValue(value)
        self._update_preview()

    def value(self) -> float:
        return self._spin.value()

    def _float_to_slider(self, value: float) -> int:
        span = self._max_float - self._min_float
        if span <= 0:
            return 0
        return round((value - self._min_float) / span * _SLIDER_STEPS)

    def _slider_to_float(self, position: int) -> float:
        span = self._max_float - self._min_float
        return self._min_float + (position / _SLIDER_STEPS) * span

    def _on_spin_changed(self, value: float) -> None:
        if self._syncing:
            return
        self._syncing = True
        self._slider.setValue(self._float_to_slider(value))
        self._syncing = False
        self._debounce.start(_PREVIEW_DEBOUNCE_MS)

    def _on_slider_changed(self, position: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        self._spin.setValue(self._slider_to_float(position))
        self._syncing = False
        self._debounce.start(_PREVIEW_DEBOUNCE_MS)

    def _update_preview(self) -> None:
        value = self._spin.value()
        wear = wear_for_float(value)
        self._wear_label.setText(f"Wear: {wear}")

        price_info = latest_price_for_wear(self._skin_id, wear)
        if price_info is None:
            self._price_label.setText("Price: —")
        else:
            price, _observed_at = price_info
            self._price_label.setText(f"Price: ${price:.2f}")

        self.valueChanged.emit(value)
