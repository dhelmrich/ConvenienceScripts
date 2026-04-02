import subprocess
import re
import sys
import numpy as np
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QPainter, QColor, QPen
import argparse

parser = argparse.ArgumentParser(description='Display WiFi link quality.')
parser.add_argument('--percent', action='store_true', help='Show link quality as percentage (0-100)')
parser.add_argument('--text', action='store_true', help='Show text labels on points')
parser.add_argument('--timeout', type=int, default=200, help='Sampling timeout in milliseconds')
args = parser.parse_args()

data = [0 for _ in range(40)]
timeout = args.timeout

class WifiWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._drag_pos = None
        self.init_ui()
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_data)
        self.timer.start(timeout)

    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(360, 300)

    def update_data(self):
        result = subprocess.run(['iwconfig', 'wlp0s20f3'], capture_output=True, text=True)
        output = result.stdout
        match = re.search(r'Link Quality=(\d+/\d+)', output)
        if match:
            link_quality = int(match.group(1).split('/')[0])
            data.append(link_quality)
        else:
            data.append(0)
        data.pop(0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        bg_color = QColor(64, 64, 64, 64)
        painter.fillRect(self.rect(), bg_color)
        
        padding = 40
        width = self.width() - padding * 2
        height = self.height() - padding * 2
        
        max_val = 100 if args.percent else 70
        min_val = 0
        
        x_step = width / (len(data) - 1)
        
        for i in range(len(data) - 1):
            x1 = int(padding + i * x_step)
            x2 = int(padding + (i + 1) * x_step)
            y1 = int(padding + height - ((data[i] - min_val) / (max_val - min_val)) * height)
            y2 = int(padding + height - ((data[i + 1] - min_val) / (max_val - min_val)) * height)
            
            c = self.get_color(data[i])
            painter.setPen(QPen(QColor(int(c[0] * 255), int(c[1] * 255), int(c[2] * 255)), 2))
            painter.drawLine(x1, y1, x2, y2)
            
            painter.setPen(QColor(int(c[0] * 255), int(c[1] * 255), int(c[2] * 255)))
            painter.drawEllipse(x1, y1, 4, 4)
        
        if args.text:
            font = painter.font()
            font.setPixelSize(10)
            painter.setFont(font)
            for i, x in enumerate(data):
                if args.percent:
                    percent = int(x)
                else:
                    percent = int(x / 70 * 100)
                if i > 0 and data[i] == data[i-1]:
                    continue
                x_pos = int(padding + i * x_step)
                y_pos = int(padding + height - ((x - min_val) / (max_val - min_val)) * height - 5)
                painter.setPen(QColor(128, 128, 128))
                painter.drawText(x_pos - 10, y_pos, f'{percent}%')
        
        y_label = 'Link Quality (%)' if args.percent else 'Link Quality'
        painter.save()
        painter.translate(15, self.height() / 2)
        painter.rotate(-90)
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(-50, 0, y_label)
        painter.restore()
        
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(padding + width // 2 - 30, self.height() - 10, 'Time')

    def get_color(self, x):
        max_val = 100 if args.percent else 70
        t = (x - 5) / 65 if not args.percent else (x - 10) / 90
        t = max(0, min(1, t))
        
        red = np.array([1.0, 0.0, 0.0])
        green = np.array([0.0, 1.0, 0.0])
        c = (1 - t) * red + t * green
        
        return np.clip(c, 0, 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            self.move(self.pos() + event.pos() - self._drag_pos)
            event.accept()

    def keyPressEvent(self, event):
        global timeout
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_Up:
            timeout = min(timeout + 50, 5000)
            self.timer.setInterval(timeout)
        elif event.key() == Qt.Key.Key_Down:
            timeout = max(timeout - 50, 50)
            self.timer.setInterval(timeout)

app = QApplication(sys.argv)
widget = WifiWidget()
widget.show()
sys.exit(app.exec())
