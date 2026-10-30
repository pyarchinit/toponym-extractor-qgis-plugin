"""Finestra non modale che mostra cosa sta succedendo durante un'operazione
lunga (estrazione, ricerca della modalità migliore): tempo trascorso, barra di
progresso e log testuale dei singoli passaggi."""

import time

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QDialog,
    QLabel,
    QVBoxLayout,
    QProgressBar,
    QListWidget,
    QPushButton,
)

from ..core.i18n import tr
from ..core.plugin_version import get_plugin_version


def _format_duration(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"


class ProgressDialog(QDialog):
    def __init__(self, parent=None, title=None, indeterminate=False):
        """`indeterminate`: barra "a scorrimento" per le operazioni di cui non
        si conosce in anticipo la durata."""
        super().__init__(parent)
        if title is None:
            title = tr("Estrazione in corso")
        self.setWindowTitle(f"Toponimi Extractor v{get_plugin_version()} - {title}")
        self.setMinimumSize(520, 360)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        self._started_at = time.monotonic()
        self._step_started_at = self._started_at

        layout = QVBoxLayout(self)

        self.current_step_label = QLabel(tr("Avvio..."))
        self.current_step_label.setWordWrap(True)
        self.current_step_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.current_step_label)

        self.elapsed_label = QLabel("")
        layout.addWidget(self.elapsed_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0 if indeterminate else 100)
        layout.addWidget(self.progress_bar)

        self.log_list = QListWidget()
        self.log_list.setWordWrap(True)
        layout.addWidget(self.log_list)

        self.cancel_btn = QPushButton(tr("Annulla"))
        layout.addWidget(self.cancel_btn)

        # Il tempo che scorre mostra che il lavoro è vivo anche quando un
        # singolo passo (una chiamata al modello AI) dura a lungo.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_elapsed)
        self._timer.start(1000)
        self._update_elapsed()

    def _update_elapsed(self):
        now = time.monotonic()
        self.elapsed_label.setText(
            tr("Tempo trascorso: {total}  ·  passo corrente da {step}").format(
                total=_format_duration(now - self._started_at),
                step=_format_duration(now - self._step_started_at),
            )
        )

    def set_progress(self, value):
        self.progress_bar.setValue(int(value))

    def add_step(self, message):
        self._step_started_at = time.monotonic()
        self.current_step_label.setText(message.strip().splitlines()[0] if message.strip() else "")
        self.log_list.addItem(f"[{_format_duration(self._step_started_at - self._started_at)}] {message}")
        self.log_list.scrollToBottom()
        self._update_elapsed()

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)
