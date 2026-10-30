"""Finestra per testi lunghi (es. il ragionamento dell'agente AI): riepilogo
breve in alto, dettaglio a scorrimento, pulsanti sempre visibili per copiare
o salvare il testo su file."""

import os

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from .settings_dialog import SETTINGS_GROUP


class ReportDialog(QDialog):
    def __init__(self, title, summary, details, parent=None, default_filename="report.txt", accept_label=None):
        """Con `accept_label` la finestra diventa una richiesta di conferma:
        il pulsante principale prende quel testo e compare "Annulla"."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 520)
        self._summary = summary
        self._details = details
        self._default_filename = default_filename

        layout = QVBoxLayout(self)

        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        self.details_edit = QPlainTextEdit()
        self.details_edit.setReadOnly(True)
        self.details_edit.setPlainText(details)
        layout.addWidget(self.details_edit, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        if accept_label:
            buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText(accept_label)
            buttons.rejected.connect(self.reject)
        copy_btn = buttons.addButton("Copia", QDialogButtonBox.ButtonRole.ActionRole)
        save_btn = buttons.addButton("Salva su file...", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(self._copy)
        save_btn.clicked.connect(self._save)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def full_text(self):
        return f"{self._summary}\n\n{self._details}"

    def _copy(self):
        QApplication.clipboard().setText(self.full_text())

    def _save(self):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        last_dir = s.value("last_output_dir", os.path.expanduser("~"), type=str)
        s.endGroup()

        path, _ = QFileDialog.getSaveFileName(
            self, "Salva report", os.path.join(last_dir, self._default_filename), "File di testo (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.full_text())
        except OSError as exc:
            QMessageBox.warning(self, "Toponimi Extractor", f"Impossibile salvare il file:\n{exc}")
