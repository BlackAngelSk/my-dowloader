"""Vector icons drawn with QPainter — no image assets, themed at runtime.

Emoji used to stand in for icons (📥 📋 🛡️ …).  They render differently on every
platform, fall back to boxes when the font is missing (which is what an
offscreen or a bare Windows install looks like), and cannot follow the accent
colour.  These glyphs are painted from primitives instead: one code path, any
size, any colour.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

#: Every icon is drawn on a 24×24 grid and scaled to the requested size.
GRID = 24.0


def _pen(color: QColor, width: float) -> QPen:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _download(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawLine(QPointF(12, 4), QPointF(12, 15))
    p.drawPath(_path([(7.5, 10.5), (12, 15), (16.5, 10.5)]))
    p.drawPath(_path([(5, 19), (19, 19)]))


def _inbox(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawPath(_path([(4, 14), (4, 18.5), (20, 18.5), (20, 14)]))
    p.drawLine(QPointF(12, 4), QPointF(12, 13.5))
    p.drawPath(_path([(8, 9.5), (12, 13.5), (16, 9.5)]))


def _clock(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawEllipse(QRectF(4.5, 4.5, 15, 15))
    p.drawPath(_path([(12, 8.5), (12, 12.4), (15, 14)]))


def _history(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawEllipse(QRectF(4.5, 4.5, 15, 15))
    p.drawPath(_path([(12, 8.5), (12, 12.4), (15, 14)]))
    p.drawPath(_path([(4.6, 8.2), (4.6, 4.6), (8.2, 4.6)]))


def _shield(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawPath(_path([(12, 3.5), (19.5, 6.5), (19.5, 12.5),
                          (12, 20.5), (4.5, 12.5), (4.5, 6.5), (12, 3.5)]))
    p.drawPath(_path([(8.8, 12), (11.2, 14.4), (15.4, 9.6)]))


def _sliders(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    for y, knob in ((7.0, 15.0), (12.0, 9.0), (17.0, 14.0)):
        p.drawLine(QPointF(4, y), QPointF(20, y))
        p.setBrush(QColor(c.red(), c.green(), c.blue()))
        p.drawEllipse(QRectF(knob - 1.9, y - 1.9, 3.8, 3.8))
        p.setBrush(Qt.BrushStyle.NoBrush)


def _sun(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawEllipse(QRectF(8.5, 8.5, 7, 7))
    for dx, dy in ((0, -7.5), (0, 7.5), (-7.5, 0), (7.5, 0),
                   (-5.3, -5.3), (5.3, 5.3), (-5.3, 5.3), (5.3, -5.3)):
        p.drawLine(QPointF(12 + dx * 0.78, 12 + dy * 0.78),
                   QPointF(12 + dx, 12 + dy))


def _moon(p: QPainter, c: QColor, w: float) -> None:
    outer = QPainterPath()
    outer.addEllipse(QRectF(4.5, 3.5, 15, 17))
    bite = QPainterPath()
    bite.addEllipse(QRectF(9.5, 1.5, 14, 15))
    p.setPen(_pen(c, w))
    p.drawPath(outer.subtracted(bite))


def _play(p: QPainter, c: QColor, w: float) -> None:
    path = QPainterPath()
    path.moveTo(8.5, 5.5)
    path.lineTo(18.0, 12.0)
    path.lineTo(8.5, 18.5)
    path.closeSubpath()
    p.setPen(_pen(c, w))
    p.setBrush(QColor(c.red(), c.green(), c.blue()))
    p.drawPath(path)


def _pause(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(c.red(), c.green(), c.blue()))
    p.drawRoundedRect(QRectF(8.2, 5.5, 2.8, 13), 1.2, 1.2)
    p.drawRoundedRect(QRectF(13.0, 5.5, 2.8, 13), 1.2, 1.2)


def _close(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawLine(QPointF(7, 7), QPointF(17, 17))
    p.drawLine(QPointF(17, 7), QPointF(7, 17))


def _folder(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawPath(_path([(4, 18.5), (4, 6.5), (10, 6.5), (12, 9),
                          (20, 9), (20, 18.5), (4, 18.5)]))


def _film(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawRoundedRect(QRectF(3.5, 6.0, 17, 12), 2.4, 2.4)
    p.drawPath(_path([(10.5, 9.5), (14.5, 12), (10.5, 14.5), (10.5, 9.5)]))


def _paste(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawRoundedRect(QRectF(6.0, 4.5, 12, 16), 2.2, 2.2)
    p.drawRoundedRect(QRectF(9.5, 2.8, 5, 3.4), 1.2, 1.2)


def _plus(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawLine(QPointF(12, 6), QPointF(12, 18))
    p.drawLine(QPointF(6, 12), QPointF(18, 12))


def _check(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawPath(_path([(5.5, 12.5), (10, 17), (18.5, 7.5)]))


def _refresh(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    path = QPainterPath()
    path.arcMoveTo(QRectF(4.5, 4.5, 15, 15), 40)
    path.arcTo(QRectF(4.5, 4.5, 15, 15), 40, 300)
    p.drawPath(path)
    p.drawPath(_path([(16.5, 2.8), (17.6, 7.4), (13.0, 6.6)]))


def _audio(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawLine(QPointF(9.5, 16.5), QPointF(9.5, 6.5))
    p.drawLine(QPointF(9.5, 6.5), QPointF(17.5, 5.0))
    p.drawLine(QPointF(17.5, 5.0), QPointF(17.5, 15.0))
    p.setBrush(QColor(c.red(), c.green(), c.blue()))
    p.drawEllipse(QRectF(5.6, 14.6, 4.0, 3.4))
    p.drawEllipse(QRectF(13.6, 13.1, 4.0, 3.4))


def _search(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawEllipse(QRectF(4.5, 4.5, 11, 11))
    p.drawLine(QPointF(14.5, 14.5), QPointF(19.5, 19.5))


def _trash(p: QPainter, c: QColor, w: float) -> None:
    p.setPen(_pen(c, w))
    p.drawLine(QPointF(4.5, 7), QPointF(19.5, 7))
    p.drawPath(_path([(9.5, 7), (9.5, 4.5), (14.5, 4.5), (14.5, 7)]))
    p.drawPath(_path([(6.5, 7), (7.5, 19.5), (16.5, 19.5), (17.5, 7)]))


def _logo(p: QPainter, c: QColor, w: float) -> None:
    """The app mark: a rounded tile with a download arrow (filled)."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(c.red(), c.green(), c.blue()))
    p.drawRoundedRect(QRectF(1.5, 1.5, 21, 21), 6.5, 6.5)
    ink = QColor(255, 255, 255, 235)
    p.setPen(_pen(ink, 2.0))
    p.drawLine(QPointF(12, 6.5), QPointF(12, 14.5))
    p.drawPath(_path([(8.4, 11.2), (12, 14.8), (15.6, 11.2)]))
    p.drawLine(QPointF(7.5, 17.5), QPointF(16.5, 17.5))


def _path(points: list[tuple[float, float]]) -> QPainterPath:
    """A polyline as a path (QPainter.drawPolyline wants points, not a path)."""
    path = QPainterPath(QPointF(*points[0]))
    for x, y in points[1:]:
        path.lineTo(x, y)
    return path


_PAINTERS = {
    "download": _download,
    "inbox": _inbox,
    "clock": _clock,
    "history": _history,
    "shield": _shield,
    "sliders": _sliders,
    "sun": _sun,
    "moon": _moon,
    "play": _play,
    "pause": _pause,
    "close": _close,
    "folder": _folder,
    "film": _film,
    "paste": _paste,
    "plus": _plus,
    "check": _check,
    "refresh": _refresh,
    "search": _search,
    "audio": _audio,
    "trash": _trash,
    "logo": _logo,
}

#: Stroke width relative to the drawing grid (1.7 at 24 px looks right).
STROKE = 1.7


def pixmap(name: str, color: str, size: int = 18) -> QPixmap:
    """Draw *name* at *size* px in *color* (device-pixel aware)."""
    painter_fn = _PAINTERS.get(name)
    if painter_fn is None:
        raise KeyError(f"unknown icon {name!r}")
    from PyQt6.QtWidgets import QApplication

    ratio = 1.0
    app = QApplication.instance()
    if app is not None:
        try:
            ratio = app.primaryScreen().devicePixelRatio() if app.primaryScreen() else 1.0
        except Exception:  # noqa: BLE001 - headless screens
            ratio = 1.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(size / GRID * ratio, size / GRID * ratio)
    painter_fn(p, QColor(color), STROKE * 1.0)
    p.end()
    return pm


def icon(name: str, color: str, size: int = 18) -> QIcon:
    """A themed icon for buttons and labels."""
    return QIcon(pixmap(name, color, size))
