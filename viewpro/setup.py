"""Setup script for ViewPro."""
from setuptools import setup, find_packages
import sys

if sys.platform == "win32":
    qt_dependency = "PySide6>=6.5.0"
else:
    qt_dependency = "PyQt6>=6.5.0"

setup(
    name="viewpro",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        qt_dependency,
    ],
    entry_points={
        "gui_scripts": [
            "viewpro=viewpro.main:main",
        ],
    },
)
