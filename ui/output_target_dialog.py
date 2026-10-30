"""Dialogo per scegliere dove salvare i toponimi PRIMA di disegnare l'area:
file GeoPackage (nuovo o esistente) e layer (nuovo o già presente nel file).

Il dialogo riparte sempre dall'ultima destinazione usata e dice in chiaro
cosa succederà ai dati: creazione di un file, creazione di un layer, oppure
aggiunta a un layer esistente (mai sovrascrittura).
"""

import os
import re

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QHBoxLayout,
    QDialogButtonBox,
    QMessageBox,
    QFileDialog,
    QComboBox,
    QRadioButton,
    QButtonGroup,
    QLabel,
)
from qgis.core import Qgis, QgsProviderRegistry, QgsVectorLayer

from .settings_dialog import SETTINGS_GROUP
from ..core.i18n import tr
from ..core.plugin_version import get_plugin_version
from ..core import geopackage_writer

DEFAULT_FILE_NAME = "toponimi_estratti.gpkg"
DEFAULT_LAYER_NAME = "toponimi"

# Lettere (anche accentate), cifre, spazio, trattino e underscore: tutto ciò
# che non crea problemi nell'indirizzo "file|layername=nome" usato da QGIS.
_VALID_LAYER_NAME = re.compile(r"^[\w][\w \-]{0,59}$")


class OutputTargetDialog(QDialog):
    """Raccoglie la destinazione dei risultati. Dopo exec(), se accettato:
    - .gpkg_path: percorso assoluto del GeoPackage;
    - .layer_name: nome del layer dei toponimi;
    - .append_to_existing: True se l'utente ha scelto un layer già presente
      (i nuovi punti si aggiungono, senza chiedere altro al salvataggio).
    """

    def __init__(self, parent=None, current_gpkg_path=None, current_layer_name=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Toponimi Extractor v{version} - Destinazione risultati").format(version=get_plugin_version()))
        self.setMinimumWidth(520)

        self.layer_name = None
        self.gpkg_path = None
        self.append_to_existing = False

        # nome -> numero di toponimi, per i layer del file su cui si può aggiungere
        self._usable_layers = {}
        # tutti i nomi di layer presenti nel file (anche non del plugin)
        self._all_layer_names = set()
        # percorso per cui sono state calcolate le scelte mostrate
        self._loaded_path = None
        # True se il file è cambiato scrivendo il percorso a mano e l'utente
        # non ha ancora visto/toccato le scelte ricalcolate per il nuovo file
        self._needs_review = False

        self._build_ui()
        self._load_last_values(current_gpkg_path, current_layer_name)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        path_layout = QHBoxLayout()
        self.gpkg_path_edit = QLineEdit()
        self.gpkg_path_edit.setPlaceholderText(tr("Percorso completo del file GeoPackage"))
        # editingFinished e non textChanged: il file va riletto quando il
        # percorso è completo, non a ogni tasto.
        self.gpkg_path_edit.editingFinished.connect(self._on_path_edited)
        browse_existing_btn = QPushButton(tr("Apri esistente..."))
        browse_existing_btn.clicked.connect(self._browse_existing_gpkg)
        browse_new_btn = QPushButton(tr("Nuovo file..."))
        browse_new_btn.clicked.connect(self._browse_new_gpkg)
        path_layout.addWidget(self.gpkg_path_edit)
        path_layout.addWidget(browse_existing_btn)
        path_layout.addWidget(browse_new_btn)
        form.addRow(tr("File GeoPackage:"), path_layout)
        layout.addLayout(form)

        self.radio_existing_layer = QRadioButton(tr("Aggiungi a un layer già presente nel file"))
        self.radio_new_layer = QRadioButton(tr("Crea un nuovo layer"))
        self.layer_choice_group = QButtonGroup(self)
        self.layer_choice_group.addButton(self.radio_existing_layer)
        self.layer_choice_group.addButton(self.radio_new_layer)
        self.radio_new_layer.setChecked(True)

        layout.addWidget(self.radio_existing_layer)
        self.existing_layer_combo = QComboBox()
        layout.addWidget(self.existing_layer_combo)

        layout.addWidget(self.radio_new_layer)
        self.layer_name_edit = QLineEdit()
        self.layer_name_edit.setPlaceholderText(tr("Nome del nuovo layer, es. toponimi_igm_montefeltro"))
        layout.addWidget(self.layer_name_edit)

        self.outcome_label = QLabel("")
        self.outcome_label.setWordWrap(True)
        self.outcome_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.outcome_label)

        self.radio_existing_layer.toggled.connect(self._update_layer_input_mode)
        for signal in (
            self.radio_existing_layer.clicked,
            self.radio_new_layer.clicked,
            self.existing_layer_combo.activated,
            self.layer_name_edit.textEdited,
        ):
            signal.connect(self._mark_reviewed)
        self.existing_layer_combo.currentIndexChanged.connect(self._update_outcome)
        self.layer_name_edit.textChanged.connect(self._update_outcome)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------ percorso

    def _current_path(self):
        """Percorso scritto nel campo, con estensione .gpkg garantita."""
        path = self.gpkg_path_edit.text().strip().strip('"')
        if path and not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        return os.path.normpath(path) if path else ""

    def _browse_existing_gpkg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Apri GeoPackage esistente"), self._current_path() or os.path.expanduser("~"),
            "GeoPackage (*.gpkg)",
        )
        if path:
            self.gpkg_path_edit.setText(os.path.normpath(path))
            self._refresh_from_path()

    def _browse_new_gpkg(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Crea nuovo GeoPackage"),
            self._current_path() or os.path.expanduser("~"),
            "GeoPackage (*.gpkg)",
            # Scegliere un file esistente qui non lo sovrascrive: ci si aggiunge un layer.
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            self.gpkg_path_edit.setText(os.path.normpath(path))
            self._refresh_from_path()

    def _mark_reviewed(self, *_):
        self._needs_review = False

    def _on_path_edited(self):
        """Percorso scritto a mano: se indica un altro file, le scelte sul
        layer vengono ricalcolate e vanno riviste prima di confermare."""
        if self._current_path() != self._loaded_path:
            self._refresh_from_path()
            self._needs_review = True

    def _read_layers(self, gpkg_path):
        """Legge dal file i nomi di tutti i layer e, tra questi, quelli su cui
        il plugin può aggiungere punti: puntuali e con i campi del plugin.
        Un layer puntuale creato da altri non viene mai proposto."""
        self._usable_layers = {}
        self._all_layer_names = set()
        if not gpkg_path or not os.path.isfile(gpkg_path):
            return
        try:
            metadata = QgsProviderRegistry.instance().providerMetadata("ogr")
            for item in metadata.querySublayers(gpkg_path):
                name = item.name()
                self._all_layer_names.add(name)
                if name.endswith(geopackage_writer.AOI_LAYER_NAME_SUFFIX):
                    continue
                layer = QgsVectorLayer(f"{gpkg_path}|layername={name}", name, "ogr")
                if (
                    layer.isValid()
                    and layer.geometryType() == Qgis.GeometryType.Point
                    and geopackage_writer.schema_is_compatible(layer)
                ):
                    self._usable_layers[name] = layer.featureCount()
        except Exception:
            pass

    def _refresh_from_path(self, preferred_layer=None):
        """Rilegge il file indicato e aggiorna le scelte possibili."""
        path = self._current_path()
        self._read_layers(path)
        self._loaded_path = path

        self.existing_layer_combo.blockSignals(True)
        self.existing_layer_combo.clear()
        for name in sorted(self._usable_layers, key=str.lower):
            self.existing_layer_combo.addItem(tr("{name}  ({count} toponimi)").format(name=name, count=self._usable_layers[name]), name)
        self.existing_layer_combo.blockSignals(False)

        has_usable = bool(self._usable_layers)
        self.radio_existing_layer.setEnabled(has_usable)
        if has_usable:
            # Se il file contiene già layer del plugin, aggiungere è il caso
            # normale: continuare lo stesso lavoro su un'altra area.
            idx = self.existing_layer_combo.findData(preferred_layer) if preferred_layer else -1
            self.existing_layer_combo.setCurrentIndex(max(idx, 0))
            self.radio_existing_layer.setChecked(True)
        else:
            self.radio_new_layer.setChecked(True)

        if not self.layer_name_edit.text().strip() or self.layer_name_edit.text().strip() in self._all_layer_names:
            self.layer_name_edit.setText(self._suggest_new_layer_name())

        self._update_layer_input_mode()

    def _suggest_new_layer_name(self):
        """Primo nome libero nel file: toponimi, toponimi_2, toponimi_3..."""
        aoi_suffix = geopackage_writer.AOI_LAYER_NAME_SUFFIX
        candidate, n = DEFAULT_LAYER_NAME, 1
        while candidate in self._all_layer_names or f"{candidate}{aoi_suffix}" in self._all_layer_names:
            n += 1
            candidate = f"{DEFAULT_LAYER_NAME}_{n}"
        return candidate

    # ------------------------------------------------------------------ layer

    def _update_layer_input_mode(self):
        use_existing = self.radio_existing_layer.isChecked()
        self.existing_layer_combo.setEnabled(use_existing)
        self.layer_name_edit.setEnabled(not use_existing)
        self._update_outcome()

    def _update_outcome(self):
        """Dice in chiaro cosa succederà con le scelte correnti."""
        path = self._current_path()
        if not path:
            self.outcome_label.setText("")
            return

        file_name = os.path.basename(path)
        if self.radio_existing_layer.isChecked() and self.existing_layer_combo.currentData():
            name = self.existing_layer_combo.currentData()
            text = tr(
                "I nuovi toponimi verranno AGGIUNTI al layer \"{name}\" di {file} "
                "({count} già presenti). Nulla viene cancellato."
            ).format(name=name, file=file_name, count=self._usable_layers.get(name, 0))
        else:
            name = self.layer_name_edit.text().strip() or "..."
            if os.path.isfile(path):
                text = tr("Verrà creato il nuovo layer \"{name}\" dentro il file esistente {file}.").format(
                    name=name, file=file_name
                )
            else:
                text = tr("Verrà creato il nuovo file {file} con il layer \"{name}\".").format(file=file_name, name=name)
        self.outcome_label.setText(text)

    # ------------------------------------------------------------------ valori iniziali e conferma

    def _load_last_values(self, current_gpkg_path, current_layer_name):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        last_dir = s.value("last_output_dir", os.path.expanduser("~"), type=str)
        last_path = s.value("last_output_gpkg", "", type=str)
        last_layer = s.value("last_output_layer", "", type=str)
        s.endGroup()

        path = current_gpkg_path or last_path or os.path.join(last_dir, DEFAULT_FILE_NAME)
        self.gpkg_path_edit.setText(os.path.normpath(path))
        self._refresh_from_path(preferred_layer=current_layer_name or last_layer)

    def _warn(self, message):
        QMessageBox.warning(self, "Toponimi Extractor", message)

    def _on_accept(self):
        # Il percorso può essere stato scritto a mano senza uscire dal campo.
        self._on_path_edited()
        if self._needs_review:
            # Le scelte qui sotto sono appena cambiate insieme al file: non si
            # conferma una destinazione che l'utente non ha ancora visto.
            self._needs_review = False
            QMessageBox.information(
                self,
                "Toponimi Extractor",
                tr(
                    "Hai indicato un altro file: ho aggiornato le scelte sul layer.\n\n"
                    "Controlla la riga in grassetto, che dice cosa succederà, poi premi di nuovo OK."
                ),
            )
            return

        # Rilettura senza toccare le scelte: il file può essere cambiato su disco.
        self._read_layers(self._current_path())
        selected_existing = self.existing_layer_combo.currentData() if self.radio_existing_layer.isChecked() else None
        if selected_existing and selected_existing not in self._usable_layers:
            self._warn(tr("Il layer scelto non è presente nel file indicato. Controlla il percorso e la scelta del layer."))
            return

        gpkg_path = self._current_path()
        if not gpkg_path or not os.path.isabs(gpkg_path):
            self._warn(
                tr("Indica il percorso completo del file GeoPackage (usa \"Apri esistente...\" o \"Nuovo file...\").")
            )
            return
        folder = os.path.dirname(gpkg_path)
        if not os.path.isdir(folder):
            self._warn(tr("La cartella non esiste:\n{folder}\n\nScegli una cartella esistente.").format(folder=folder))
            return
        if os.path.isdir(gpkg_path):
            self._warn(tr("Il percorso indicato è una cartella, non un file."))
            return

        if selected_existing:
            layer_name = selected_existing
            append = True
        else:
            layer_name = self.layer_name_edit.text().strip()
            append = False
            if not layer_name:
                self._warn(tr("Scrivi il nome del nuovo layer."))
                return
            if not _VALID_LAYER_NAME.match(layer_name):
                self._warn(
                    tr(
                        "Il nome del layer può contenere solo lettere, cifre, spazi, trattini e underscore "
                        "(massimo 60 caratteri) e deve iniziare con una lettera o una cifra."
                    )
                )
                return
            if layer_name.endswith(geopackage_writer.AOI_LAYER_NAME_SUFFIX):
                self._warn(
                    tr(
                        "I nomi che finiscono con \"{suffix}\" sono riservati "
                        "ai layer delle aree di estrazione. Scegli un altro nome."
                    ).format(suffix=geopackage_writer.AOI_LAYER_NAME_SUFFIX)
                )
                return
            if layer_name in self._usable_layers:
                choice = QMessageBox.question(
                    self,
                    "Toponimi Extractor",
                    tr(
                        "Nel file esiste già un layer \"{name}\" con {count} toponimi.\n\n"
                        "Vuoi AGGIUNGERE i nuovi toponimi a quel layer?\n"
                        "(Per tenerli separati rispondi No e scegli un altro nome.)"
                    ).format(name=layer_name, count=self._usable_layers[layer_name]),
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    defaultButton=QMessageBox.StandardButton.No,
                )
                if choice != QMessageBox.StandardButton.Yes:
                    return
                append = True
            elif layer_name in self._all_layer_names or geopackage_writer.aoi_layer_name(layer_name) in self._all_layer_names:
                self._warn(
                    tr(
                        "Nel file esiste già un layer \"{name}\" (o il suo layer delle aree) che non è "
                        "un layer di toponimi di questo plugin. Per non toccarlo, scegli un altro nome."
                    ).format(name=layer_name)
                )
                return

        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        s.setValue("last_output_dir", folder)
        s.setValue("last_output_gpkg", gpkg_path)
        s.setValue("last_output_layer", layer_name)
        s.endGroup()

        self.layer_name = layer_name
        self.gpkg_path = gpkg_path
        self.append_to_existing = append
        self.accept()
