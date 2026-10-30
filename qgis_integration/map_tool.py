"""Strumento mappa per disegnare un rettangolo (AOI) con click-and-drag."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand


class RectangleMapTool(QgsMapTool):
    """Strumento interattivo: click per iniziare, drag, rilascio per confermare
    il rettangolo. Emette `rectangleCreated(QgsRectangle)` al rilascio.
    """

    rectangleCreated = pyqtSignal(QgsRectangle)
    canceled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 0, 0, 80))
        self.rubber_band.setWidth(1)
        self.start_point = None
        self.is_dragging = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasPressEvent(self, event):
        self.start_point = self.toMapCoordinates(event.pos())
        self.is_dragging = True
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event):
        if not self.is_dragging or self.start_point is None:
            return
        current_point = self.toMapCoordinates(event.pos())
        self._update_rubber_band(self.start_point, current_point)

    def canvasReleaseEvent(self, event):
        if not self.is_dragging or self.start_point is None:
            return
        end_point = self.toMapCoordinates(event.pos())
        self.is_dragging = False

        rect = QgsRectangle(self.start_point, end_point)
        rect.normalize()

        self.start_point = None
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

        if rect.width() > 0 and rect.height() > 0:
            self.rectangleCreated.emit(rect)

    def _update_rubber_band(self, p1, p2):
        rect = QgsRectangle(p1, p2)
        rect.normalize()
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        points = [
            rect.xMinimum(), rect.yMinimum(),
            rect.xMaximum(), rect.yMinimum(),
            rect.xMaximum(), rect.yMaximum(),
            rect.xMinimum(), rect.yMaximum(),
        ]
        from qgis.core import QgsPointXY

        pts = [QgsPointXY(points[i], points[i + 1]) for i in range(0, len(points), 2)]
        for pt in pts:
            self.rubber_band.addPoint(pt, False)
        self.rubber_band.closePoints(True)
        self.rubber_band.show()

    def keyPressEvent(self, event):
        # Esc abbandona la selezione, anche a metà trascinamento.
        if event.key() == Qt.Key.Key_Escape:
            self.is_dragging = False
            self.start_point = None
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self.canceled.emit()

    def deactivate(self):
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()
