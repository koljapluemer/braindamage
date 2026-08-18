"""Shared metric-card summary panel: Input Cost / Expected Value / ROI / CVaR
(5%), plus a favorite indicator and a missing-price warning line. Used by both
the live Contract Builder result view and the historical Contracts detail
dialog, so CVaR display logic lives in exactly one place -- the builder's live
summary previously never surfaced CVaR at all, even though it was already
being computed and stored on every simulated Contract.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from ... import steam_fees


class _MetricCard(QFrame):
    def __init__(self, label: str, tooltip: str, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("QFrame { border: 1px solid palette(mid); border-radius: 6px; padding: 4px; }")
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)

        self._value_label = QLabel("—")
        self._value_label.setStyleSheet("font-size: 15pt; font-weight: 600; border: none;")
        caption = QLabel(f"{label}  ⓘ")
        caption.setStyleSheet("color: palette(mid); border: none; text-decoration: underline dotted;")

        layout = QVBoxLayout(self)
        layout.addWidget(caption)
        layout.addWidget(self._value_label)

    def set_value(self, text: str) -> None:
        self._value_label.setText(text)


class ResultSummaryPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        fee_pct = f"{steam_fees.NOMINAL_CUT_OF_GROSS:.0%}"
        self._input_cost = _MetricCard(
            "Input Cost",
            "Total market cost of the 10 input skins, at current prices for each one's exact wear. "
            "Does not include Steam's sell fee — that's only charged when you sell an output.",
        )
        self._expected_value = _MetricCard(
            "Expected Value",
            "Probability-weighted average profit: the sum of every possible outcome's (probability × net sell "
            "price after Steam's real per-sale fee — 5% Steam + 10% game fee, computed cent-exact the way Steam "
            f"itself computes it, about {fee_pct} of gross for most prices), minus Input Cost. Positive means "
            "the contract is profitable on average; it says nothing about how risky any single roll is.",
        )
        self._roi = _MetricCard(
            "ROI",
            "Return on investment: Expected Value ÷ Input Cost, as a percentage. "
            "How much profit you'd expect to make relative to what you spent, on average across many rolls.",
        )
        self._cvar = _MetricCard(
            "CVaR (5%)",
            "Conditional Value at Risk: the average profit across just the worst 5% of possible outcomes "
            "(weighted by probability). A downside-risk measure — a very negative CVaR means the bad-luck "
            "rolls are painful even if Expected Value looks fine.",
        )

        self._favorite_label = QLabel("")
        self._warning_label = QLabel("")
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet("color: #b45309;")

        grid = QGridLayout()
        grid.addWidget(self._input_cost, 0, 0)
        grid.addWidget(self._expected_value, 0, 1)
        grid.addWidget(self._roi, 0, 2)
        grid.addWidget(self._cvar, 0, 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._favorite_label)
        layout.addLayout(grid)
        layout.addWidget(self._warning_label)

    def clear(self) -> None:
        for card in (self._input_cost, self._expected_value, self._roi, self._cvar):
            card.set_value("—")
        self._favorite_label.setText("")
        self._warning_label.setText("")

    def set_result(
        self,
        *,
        input_cost: float,
        expected_value: float,
        roi: float | None,
        cvar_5pct: float | None,
        favorite: bool | None = None,
        missing_input_count: int = 0,
        missing_output_count: int = 0,
    ) -> None:
        self._input_cost.set_value(f"${input_cost:.2f}")
        self._expected_value.set_value(f"${expected_value:+.2f}")
        self._roi.set_value(f"{roi:.1%}" if roi is not None else "—")
        self._cvar.set_value(f"${cvar_5pct:.2f}" if cvar_5pct is not None else "—")

        if favorite is None:
            self._favorite_label.setText("")
        else:
            self._favorite_label.setText("★ favorited" if favorite else "☆ not favorited")

        if missing_input_count or missing_output_count:
            self._warning_label.setText(
                f"Missing prices: {missing_input_count} input, {missing_output_count} output"
            )
        else:
            self._warning_label.setText("")
