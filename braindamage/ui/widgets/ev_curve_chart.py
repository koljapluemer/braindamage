"""EV-vs-average-input-float chart for the contract detail dialog.

Renders `Contract.ev_curve` (see braindamage.tradeup.simulate_ev_curve) -- a
line through each sample's expected value plus a vertical error bar (± the
sample's outcome-price stdev) drawn every few samples. All 100 samples get
individually priced and feed the line, but whiskering every single one of them
would visually merge into an unreadable smear at that density, so only every
`_ERROR_BAR_STRIDE`-th sample gets a drawn whisker -- the line itself still
carries the full-resolution data.

QtCharts has no built-in error-bar series, so the whiskers are hand-drawn with
a QPainter directly on the view's viewport, on top of the normal chart paint,
using QChart.mapToPosition to convert data coordinates to pixels.
"""

from __future__ import annotations

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

_LINE_COLOR = QColor("#2f6fed")
_ERROR_BAR_COLOR = QColor(47, 111, 237, 150)
_ERROR_BAR_STRIDE = 5
_ERROR_BAR_CAP_HALF_WIDTH = 4.0


class EvCurveChart(QChartView):
    def __init__(self, parent: QWidget | None = None) -> None:
        chart = QChart()
        chart.legend().hide()
        chart.setTitle("Expected value vs. average input float")
        super().__init__(chart, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setMinimumHeight(220)
        self._points: list[dict] = []
        self._series: QLineSeries | None = None

    def set_points(self, points: list[dict]) -> None:
        self._points = points
        chart = self.chart()
        chart.removeAllSeries()
        for axis in list(chart.axes()):
            chart.removeAxis(axis)
        self._series = None

        if not points:
            self.viewport().update()
            return

        series = QLineSeries()
        series.setPen(QPen(_LINE_COLOR, 2))
        for p in points:
            series.append(p["avg_float"], p["expected_value"])
        chart.addSeries(series)

        x_values = [p["avg_float"] for p in points]
        y_lows = [p["expected_value"] - p["stdev"] for p in points]
        y_highs = [p["expected_value"] + p["stdev"] for p in points]
        y_min, y_max = min(y_lows), max(y_highs)
        y_pad = (y_max - y_min) * 0.08 or 1.0

        x_axis = QValueAxis()
        x_axis.setTitleText("Average input float (normalized 0-1, per-skin)")
        x_axis.setRange(min(x_values), max(x_values))
        y_axis = QValueAxis()
        y_axis.setTitleText("Expected value ($)")
        y_axis.setRange(y_min - y_pad, y_max + y_pad)

        chart.addAxis(x_axis, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(x_axis)
        series.attachAxis(y_axis)

        self._series = series
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._points or self._series is None:
            return

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(_ERROR_BAR_COLOR, 1.5))

        chart = self.chart()
        series = self._series
        for point in self._points[::_ERROR_BAR_STRIDE]:
            x = point["avg_float"]
            ev = point["expected_value"]
            stdev = point["stdev"]
            top = chart.mapToPosition(QPointF(x, ev + stdev), series)
            bottom = chart.mapToPosition(QPointF(x, ev - stdev), series)
            painter.drawLine(top, bottom)
            painter.drawLine(
                QPointF(top.x() - _ERROR_BAR_CAP_HALF_WIDTH, top.y()),
                QPointF(top.x() + _ERROR_BAR_CAP_HALF_WIDTH, top.y()),
            )
            painter.drawLine(
                QPointF(bottom.x() - _ERROR_BAR_CAP_HALF_WIDTH, bottom.y()),
                QPointF(bottom.x() + _ERROR_BAR_CAP_HALF_WIDTH, bottom.y()),
            )
        painter.end()
