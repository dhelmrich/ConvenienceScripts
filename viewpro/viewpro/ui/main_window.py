"""Main window UI for ViewPro."""

import sys
import os
import logging
from pathlib import Path
from ..qt_compat import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLineEdit,
    QTextEdit,
    QLabel,
    QRadioButton,
    QButtonGroup,
    QFileDialog,
    QScrollArea,
    QFrame,
    QGridLayout,
    QMessageBox,
    QDialog,
    QSizePolicy,
    QAbstractItemView,
    QMenu,
    QToolButton,
    Qt,
    QTimer,
    QSize,
    QPixmap,
)
from ..project_manager import ProjectManager
from ..storage.json_store import JsonStore

logger = logging.getLogger(__name__)


class ProjectCard(QFrame):
    """Individual project display card."""

    def __init__(self, project: dict, parent_window, app_dir: Path = None, parent=None):
        super().__init__(parent)
        self.project = project
        self._parent_window = parent_window
        self._app_dir = app_dir
        self.setFixedSize(360, 120)
        self.setFrameStyle(QFrame.Shape.Box | QFrame.Shadow.Raised)
        self.setLineWidth(1)
        self._selected = False
        self._update_edit_button = None
        self._close_timer = None
        logger.debug(
            f"ProjectCard created for project: {project.get('title', 'Unknown')}"
        )

        layout = QHBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(10)

        logo_label = QLabel()
        logo_label.setFixedSize(100, 100)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_label.setText("No Logo")
        logo_label.setStyleSheet("background-color: transparent; color: #999;")

        logo_path = project.get("logo")
        logo_abs_path = None
        if logo_path:
            logo_abs_path = Path(self._app_dir) / logo_path if self._app_dir else None
            logger.debug(f"Attempting to load logo from: {logo_abs_path}")
            if logo_abs_path and logo_abs_path.exists():
                pixmap = QPixmap(str(logo_abs_path))
                if not pixmap.isNull():
                    scaled_pixmap = pixmap.scaled(
                        100,
                        100,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    logo_label.setPixmap(scaled_pixmap)
                    logger.debug(f"Logo loaded successfully: {logo_abs_path}")

        layout.addWidget(logo_label)

        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel(project["title"])
        title.setStyleSheet("font-size: 11pt; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        info_layout.addWidget(title)

        if project.get("description"):
            desc = QLabel(project["description"])
            desc.setWordWrap(True)
            desc.setStyleSheet("font-size: 10pt; color: #555;")
            desc.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            info_layout.addWidget(desc)

        layout.addLayout(info_layout)

        layout.addStretch()

        self.setLayout(layout)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background-color: transparent;")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def contextMenuEvent(self, event):
        """Toggle selection on right-click."""
        logger.debug(f"Right-click on card: {self.project.get('title', 'Unknown')}")
        if self._selected:
            self.deselect()
        else:
            self.select()

    def mouseDoubleClickEvent(self, event):
        """Handle double click to open without closing window."""
        logger.debug(
            f"Mouse double click on card: {self.project.get('title', 'Unknown')}, button: {event.button()}"
        )
        if event.button() == Qt.MouseButton.LeftButton:
            logger.debug(f"Opening project: {self.project.get('title', 'Unknown')}")
            # make a sict and trigger _open_selected_projects
            self._parent_window.open_project(self.project)
            self._parent_window._check_close_window()

    def mousePressEvent(self, event):
        logger.debug(
            f"Mouse press on card: {self.project.get('title', 'Unknown')}, button: {event.button()}"
        )
        if event.button() == Qt.MouseButton.LeftButton:
            logger.debug(f"Opening project: {self.project.get('title', 'Unknown')}")
            self._parent_window.open_project(self.project)
            self._parent_window._check_close_window()

    def select(self):
        self._selected = True
        self.setStyleSheet("border: 2px solid #0078d4;")
        logger.debug(f"Card selected: {self.project.get('title', 'Unknown')}")
        if self._update_edit_button:
            self._update_edit_button()

    def deselect(self):
        self._selected = False
        self.setStyleSheet("")
        logger.debug(f"Card deselected: {self.project.get('title', 'Unknown')}")
        if self._update_edit_button:
            self._update_edit_button()

    def is_selected(self):
        return self._selected

    def _edit_project(self):
        """Show dialog to edit project."""
        if self._parent_window is None:
            return
        dialog = EditProjectDialog(self.project, self._parent_window)
        if dialog.exec():
            title = dialog.title_input.text().strip()
            description = dialog.desc_input.toPlainText().strip()
            logo = dialog.logo_input.text().strip() or None
            if title:
                projects = self._parent_window.project_manager.get_projects()
                for i, proj in enumerate(projects):
                    if proj["path"] == self.project["path"]:
                        projects[i]["title"] = title
                        projects[i]["description"] = description
                        projects[i]["logo"] = logo
                        break

                self._parent_window.project_manager.store.save({"projects": projects})

                self._parent_window._refresh_projects()
            QMessageBox.information(self, "Success", "Project updated successfully!")

    def _update_edit_button(self, window):
        """Update edit button state based on selection."""
        selected_count = sum(
            1
            for i in range(window.projects_layout.count())
            if window.projects_layout.itemAt(i)
            and window.projects_layout.itemAt(i).widget()
            and window.projects_layout.itemAt(i).widget().is_selected()
        )
        window.edit_button.setEnabled(selected_count == 1)


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, project_manager: ProjectManager):
        super().__init__()
        self.project_manager = project_manager
        self.store = project_manager.store
        self.setWindowTitle("ViewPro - Project Manager")
        self.setMinimumSize(800, 600)
        self.resize(1000, 600)
        self.closeTimeout = 500
        logger.info("MainWindow initialization started")

        self._build_ui()
        self._load_projects()
        logger.info("MainWindow initialization completed")

    def _build_ui(self):
        """Build the user interface."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        central_widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        main_layout = QVBoxLayout(central_widget)

        top_layout = QHBoxLayout()

        open_group = QButtonGroup(self)
        self.open_code_radio = QRadioButton("Open in Code Editor")
        self.open_opencode_radio = QRadioButton("Open in Opencode")
        self.open_terminal_radio = QRadioButton("Open in Terminal")
        self.open_code_radio.setChecked(True)

        open_group.addButton(self.open_code_radio)
        open_group.addButton(self.open_opencode_radio)
        open_group.addButton(self.open_terminal_radio)

        top_layout.addWidget(self.open_code_radio)
        top_layout.addWidget(self.open_opencode_radio)
        top_layout.addWidget(self.open_terminal_radio)
        top_layout.addStretch()

        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(self._open_selected_projects)
        top_layout.addWidget(self.open_button)

        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self._edit_project)
        self.edit_button.setEnabled(False)
        top_layout.addWidget(self.edit_button)

        self.add_button = QPushButton("Add Project")
        self.add_button.clicked.connect(self._add_project)
        top_layout.addWidget(self.add_button)

        main_layout.addLayout(top_layout)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.scroll_area.setMinimumHeight(400)

        self.projects_container = QWidget()
        self.projects_layout = QGridLayout()
        self.projects_layout.setContentsMargins(10, 10, 10, 10)
        self.projects_layout.setHorizontalSpacing(20)
        self.projects_layout.setVerticalSpacing(20)
        self.projects_layout.setColumnMinimumWidth(0, 380)
        self.projects_layout.setColumnMinimumWidth(1, 380)
        self.projects_container.setLayout(self.projects_layout)

        self.scroll_area.setWidget(self.projects_container)
        main_layout.addWidget(self.scroll_area)

    def _load_projects(self):
        """Load and display projects."""
        projects = self.project_manager.get_projects()
        app_dir = self.store._get_app_dir()
        logger.debug(f"Loading projects with app_dir: {app_dir}")

        self.projects_container.setMinimumWidth(760)

        row_count = (len(projects) + 1) // 2
        card_height = 120
        spacing = 20
        margin = 20
        total_height = row_count * (card_height + spacing) + margin
        self.projects_container.setMinimumHeight(total_height)

        for i, project in enumerate(projects):
            row = i // 2
            col = i % 2
            card = ProjectCard(project, self, app_dir)
            card._update_edit_button = self._update_edit_button
            self.projects_layout.addWidget(card, row, col)
        if projects:
            logger.debug(f"Added {len(projects)} project cards to grid")

    def _add_project(self):
        """Show dialog to add a new project."""
        logger.info("_add_project button clicked")
        dialog = AddProjectDialog(self)
        if dialog.exec():
            path = dialog.path_input.text()
            title = dialog.title_input.text()
            description = dialog.desc_input.toPlainText()
            logo = dialog.logo_input.text().strip() or None

            if self.project_manager.add_project(path, title, description, logo):
                self._refresh_projects()
                QMessageBox.information(self, "Success", "Project added successfully!")
            else:
                QMessageBox.warning(self, "Error", "Invalid project data")

    def _edit_project(self):
        """Show dialog to edit selected project."""
        logger.info("_edit_project button clicked")
        selected_cards = []
        for i in range(self.projects_layout.count()):
            item = self.projects_layout.itemAt(i)
            if item and item.widget() and item.widget().is_selected():
                selected_cards.append(item.widget())

        if len(selected_cards) == 1:
            card = selected_cards[0]
            dialog = EditProjectDialog(card.project, self)
            dialog._card = card
            if dialog.exec():
                title = dialog.title_input.text().strip()
                description = dialog.desc_input.toPlainText().strip()
                logo = dialog.logo_input.text().strip() or None

                if title:
                    projects = self.project_manager.get_projects()
                    for i, proj in enumerate(projects):
                        if proj["path"] == card.project["path"]:
                            projects[i]["title"] = title
                            projects[i]["description"] = description
                            projects[i]["logo"] = logo
                            break

                    self.project_manager.store.save({"projects": projects})

                    self._refresh_projects()
                    QMessageBox.information(
                        self, "Success", "Project updated successfully!"
                    )

    def _refresh_projects(self):
        """Refresh the project display."""
        while self.projects_layout.count():
            child = self.projects_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self._load_projects()

    def _update_edit_button(self):
        """Update edit button state based on selection."""
        selected_count = sum(
            1
            for i in range(self.projects_layout.count())
            if self.projects_layout.itemAt(i)
            and self.projects_layout.itemAt(i).widget()
            and self.projects_layout.itemAt(i).widget().is_selected()
        )
        self.edit_button.setEnabled(selected_count == 1)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
            self._open_selected_projects()

    def open_project(self, project: dict):
        """Open project based on selected mode."""
        path = project["path"]
        mode = self.get_open_mode()
        logger.info(
            f"Opening project '{project.get('title', 'Unknown')}' in {mode} mode, path: {path}"
        )

        self.store.update_last_clicked(path)

        if mode == "opencode":
            if os.name == "nt":
                os.system(f'wt -d "{path}" opencode-cli .')
            else:
                os.system(
                    f'nohup konsole --new-tab --workdir "{path}" -e sh -c "cd {path} && opencode-cli .; exec bash" > /dev/null 2>&1 &'
                )
            logger.info(f"opencode-cli launched for {path}")
        elif mode == "terminal":
            if os.name == "nt":
                os.system(f'start "" cmd /k "cd /d "{path}""')
            else:
                os.system(
                    f'nohup konsole --new-tab --workdir "{path}" -e bash > /dev/null 2>&1 &'
                )
            logger.info(f"Terminal launched for {path}")
        else:
            result = os.system(f'code "{path}"')
            logger.info(f"code command returned: {result}")

    def _open_selected_projects(self):
        """Open all selected projects."""
        if not QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            logger.info(
                f"_open_selected_projects: opening selected projects and will close window after {self.closeTimeout} ms"
            )
        else:
            logger.info(
                "_open_selected_projects: opening selected projects without closing window (Control key held)"
            )
        selected_count = 0
        for i in range(self.projects_layout.count()):
            item = self.projects_layout.itemAt(i)
            if item and item.widget():
                card = item.widget()
                if card.is_selected():
                    selected_count += 1
                    self.open_project(card.project)
        logger.info(f"_open_selected_projects: opened {selected_count} projects")
        self._check_close_window()

    def _check_close_window(self):
        """Check if window should close after operations."""
        if not QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            self._close_timer = QTimer()
            self._close_timer.setSingleShot(True)
            self._close_timer.timeout.connect(self.close)
            self._close_timer.start(self.closeTimeout)

    def get_open_mode(self) -> str:
        """Get the selected open mode: 'code', 'opencode', or 'terminal'."""
        if self.open_opencode_radio.isChecked():
            return "opencode"
        if self.open_terminal_radio.isChecked():
            return "terminal"
        return "code"


class AddProjectDialog(QDialog):
    """Dialog for adding a new project."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Project")
        self.setMinimumSize(500, 300)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Project Folder:"))
        self.path_input = QLineEdit()
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_folder)

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(browse_btn)
        layout.addLayout(path_layout)

        layout.addWidget(QLabel("Title:"))
        self.title_input = QLineEdit()
        layout.addWidget(self.title_input)

        layout.addWidget(QLabel("Description:"))
        self.desc_input = QTextEdit()
        self.desc_input.setFixedHeight(80)
        layout.addWidget(self.desc_input)

        layout.addWidget(QLabel("Logo (optional):"))
        self.logo_input = QLineEdit()
        logo_browse = QPushButton("Browse")
        logo_browse.clicked.connect(self._browse_logo)

        logo_layout = QHBoxLayout()
        logo_layout.addWidget(self.logo_input)
        logo_layout.addWidget(logo_browse)
        layout.addLayout(logo_layout)

        self.ok_button = QPushButton("Add")
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

    def _browse_folder(self):
        """Open folder browser."""
        folder = QFileDialog.getExistingDirectory(self, "Select Project Folder")
        if folder:
            self.path_input.setText(folder)

    def _browse_logo(self):
        """Open logo image browser."""
        filters = "Images (*.png *.jpg *.jpeg *.bmp);;All Files (*)"
        logo_path, _ = QFileDialog.getOpenFileName(
            self, "Select Logo Image", "", filters
        )
        if logo_path:
            self.logo_input.setText(logo_path)


class EditProjectDialog(QDialog):
    """Dialog for editing an existing project."""

    def __init__(self, project: dict, parent=None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Edit Project")
        self.setMinimumSize(500, 300)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Project Folder:"))
        self.path_input = QLineEdit(project.get("path", ""))
        self.path_input.setEnabled(False)
        layout.addWidget(self.path_input)

        layout.addWidget(QLabel("Title:"))
        self.title_input = QLineEdit(project.get("title", ""))
        layout.addWidget(self.title_input)

        layout.addWidget(QLabel("Description:"))
        self.desc_input = QTextEdit()
        self.desc_input.setFixedHeight(80)
        self.desc_input.setPlainText(project.get("description", ""))
        layout.addWidget(self.desc_input)

        layout.addWidget(QLabel("Logo (optional):"))
        self.logo_input = QLineEdit(project.get("logo", "") or "")
        logo_browse = QPushButton("Browse")
        logo_browse.clicked.connect(self._browse_logo)

        logo_layout = QHBoxLayout()
        logo_layout.addWidget(self.logo_input)
        logo_layout.addWidget(logo_browse)
        layout.addLayout(logo_layout)

        self.ok_button = QPushButton("Save")
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

    def _browse_logo(self):
        """Open logo image browser."""
        filters = "Images (*.png *.jpg *.jpeg *.bmp);;All Files (*)"
        logo_path, _ = QFileDialog.getOpenFileName(
            self, "Select Logo Image", "", filters
        )
        if logo_path:
            self.logo_input.setText(logo_path)


def main():
    """Main entry point."""
    app = QApplication(sys.argv)

    store = JsonStore()
    manager = ProjectManager(store)
    window = MainWindow(manager)
    window.show()

    sys.exit(app.exec())
