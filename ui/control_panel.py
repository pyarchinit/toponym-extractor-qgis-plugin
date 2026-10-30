"""Pannello dockable di controllo del plugin: raccoglie tutti i pulsanti
(le stesse QAction della barra dedicata) e mostra lo stato corrente
(layer attivo, area selezionata, destinazione di output)."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QGroupBox,
    QLabel,
    QToolButton,
    QSizePolicy,
)

from ..core.plugin_version import get_plugin_version


class ControlPanelDock(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(f"Toponimi Extractor v{get_plugin_version()}", parent)
        self.setObjectName("ToponimiExtractorControlPanel")

        body = QWidget()
        layout = QVBoxLayout(body)

        status_box = QGroupBox("Stato")
        status_layout = QVBoxLayout(status_box)
        self.layer_label = QLabel()
        self.area_label = QLabel()
        self.output_label = QLabel()
        self.task_label = QLabel()
        for label in (self.layer_label, self.area_label, self.output_label, self.task_label):
            label.setWordWrap(True)
            status_layout.addWidget(label)
        layout.addWidget(status_box)

        self.buttons_box = QGroupBox("Azioni")
        self.buttons_layout = QVBoxLayout(self.buttons_box)
        layout.addWidget(self.buttons_box)

        layout.addStretch(1)
        self.setWidget(body)

    def add_action_button(self, action):
        """Aggiunge un pulsante a tutta larghezza collegato a `action`:
        testo, icona e stato abilitato restano sincronizzati con la barra."""
        button = QToolButton()
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setMinimumHeight(30)
        self.buttons_layout.addWidget(button)
        return button

    def set_status(self, layer_text, area_text, output_text, task_text=""):
        self.layer_label.setText(f"Layer attivo: {layer_text}")
        self.area_label.setText(f"Area: {area_text}")
        self.output_label.setText(f"Output: {output_text}")
        self.task_label.setText(task_text)
        self.task_label.setVisible(bool(task_text))
