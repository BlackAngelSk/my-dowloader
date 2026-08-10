"""Real-time speed graph widget using QPainter."""

from __future__ import annotations

from collections import deque
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget


class SpeedGraph(QWidget):
    """A simple real-time speed graph drawn with QPainter."""

    MAX_POINTS = 120  # 2 minutes at 1 Hz

    def __init__(self, parent: QWidget | None = None, accent_color: str = "#3B82F6") -> None:
        super().__init__(parent)
        self._data: deque[float] = deque(maxlen=self.MAX_POINTS)
        self._max_speed: float = 1.0
        self._accent = QColor(accent_color)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def add_sample(self, speed_bps: float) -> None:
        self._data.append(speed_bps)
        if speed_bps > self._max_speed:
            self._max_speed = speed_bps * 1.2
        self.update()

    def set_accent_color(self, color: str) -> None:
        self._accent = QColor(color)

    def paintEvent(self, a0) -> None:
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        padding = 4

        # Background
        painter.fillRect(0, 0, w, h, QColor(0, 0, 0, 0))

        # Grid lines
        painter.setPen(QPen(QColor(100, 100, 100, 30), 1, Qt.PenStyle.DashLine))
        for i in range(1, 4):
            y = int(h * i / 4)
            painter.drawLine(padding, y, w - padding, y)

        # Build path
        points = list(self._data)
        n = len(points)
        if n < 2:
            painter.end()
            return

        step = (w - 2 * padding) / max(1, self.MAX_POINTS - 1)
        max_val = max(self._max_speed, 1.0)

        # Filled area
        from PyQt6.QtGui import QPolygonF
        from PyQt6.QtCore import QPointF
        poly = [QPointF(padding, h)]
        for i, val in enumerate(points):
            x = padding + i * step
            y = h - (val / max_val) * (h - 2 * padding)
            poly.append(QPointF(x, y))
        poly.append(QPointF(padding + (n - 1) * step, h))

        fill_color = QColor(self._accent)
        fill_color.setAlpha(40)
        painter.setBrush(QBrush(fill_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF(poly))

        # Line
        painter.setPen(QPen(self._accent, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path_poly = []
        for i, val in enumerate(points):
            x = padding + i * step
            y = h - (val / max_val) * (h - 2 * padding)
            path_poly.append(QPointF(x, y))
        painter.drawPolyline(QPolygonF(path_poly))

        painter.end()
