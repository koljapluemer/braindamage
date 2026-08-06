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
_POSITIVE_COLOR = QColor("#16a34a")
_GUARANTEED_COLOR = QColor("#9333ea")
_ZERO_COLOR = QColor("#6b7280")
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
        self.setMinimumHeight(360)
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
        for left, right in zip(points, points[1:]):
            segment = QLineSeries()
            color = _GUARANTEED_COLOR if left.get("worst_profit", -1) >= 0 else _POSITIVE_COLOR if left["expected_value"] >= 0 else _LINE_COLOR
            segment.setPen(QPen(color, 2.5))
            segment.append(left.get("raw_avg_float", left["avg_float"]), left["expected_value"])
            segment.append(right.get("raw_avg_float", right["avg_float"]), right["expected_value"])
            chart.addSeries(segment)
        # Keep one stable reference series for coordinate mapping/error bars.
        series.setPen(QPen(Qt.PenStyle.NoPen))
        for p in points:
            series.append(p.get("raw_avg_float", p["avg_float"]), p["expected_value"])
        chart.addSeries(series)

        x_values = [p.get("raw_avg_float", p["avg_float"]) for p in points]
        y_lows = [p["expected_value"] - p["stdev"] for p in points]
        y_highs = [p["expected_value"] + p["stdev"] for p in points]
        y_min, y_max = min(min(y_lows), 0.0), max(max(y_highs), 0.0)
        y_pad = (y_max - y_min) * 0.08 or 1.0

        x_axis = QValueAxis()
        x_axis.setTitleText("Average input skin float")
        x_axis.setRange(min(x_values), max(x_values))
        y_axis = QValueAxis()
        y_axis.setTitleText("Expected value ($)")
        y_axis.setRange(y_min - y_pad, y_max + y_pad)

        chart.addAxis(x_axis, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignmentFlag.AlignLeft)
        for chart_series in chart.series():
            chart_series.attachAxis(x_axis)
            chart_series.attachAxis(y_axis)

        zero = QLineSeries()
        zero.setName("EV = $0")
        zero.setPen(QPen(_ZERO_COLOR, 1.5, Qt.PenStyle.DashLine))
        zero.append(min(x_values), 0.0)
        zero.append(max(x_values), 0.0)
        chart.addSeries(zero)
        zero.attachAxis(x_axis)
        zero.attachAxis(y_axis)

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
            x = point.get("raw_avg_float", point["avg_float"])
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
