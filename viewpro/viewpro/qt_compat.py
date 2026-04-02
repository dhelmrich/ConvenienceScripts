"""Compatibility layer for Qt bindings."""
import sys

if sys.platform == "win32":
    from PySide6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,\
        QPushButton, QLineEdit, QTextEdit, QLabel, QRadioButton, QButtonGroup,\
        QFileDialog, QScrollArea, QFrame, QGridLayout, QMessageBox, QDialog,\
        QSizePolicy, QAbstractItemView, QMenu, QToolButton
    from PySide6.QtCore import Qt, QTimer, QSize, QObject, Signal
    from PySide6.QtGui import QPixmap, QAction
else:
    from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,\
        QPushButton, QLineEdit, QTextEdit, QLabel, QRadioButton, QButtonGroup,\
        QFileDialog, QScrollArea, QFrame, QGridLayout, QMessageBox, QDialog,\
        QSizePolicy, QAbstractItemView, QMenu, QToolButton
    from PyQt6.QtCore import Qt, QTimer, QSize, QObject
    from PyQt6.QtCore import pyqtSignal as Signal
    from PyQt6.QtGui import QPixmap, QAction
