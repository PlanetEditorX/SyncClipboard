"""Qt 主题：调色板、QSS、开关控件、图标。

配色沿用原 CustomTkinter 版那套（深色 #080D18 / 浅色 #F4F7FB），
换成 Qt 后可以真正做出 Win11 那种扁平卡片 + 细边框的效果。

Qt 没有开关（switch）控件，CustomTkinter 有，所以这里自绘一个
ToggleSwitch：圆角轨道 + 带缓动的圆点，视觉上比 QCheckBox 更贴近现代桌面应用。
"""

import logging
import sys

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from gui.icon_theme import icon_path

logger = logging.getLogger("gui")

FONT_FAMILY = "Microsoft YaHei UI"
FONT_FALLBACK = "Segoe UI Variable, Segoe UI, sans-serif"

LIGHT = {
    "bg": "#F4F7FB",
    "card": "#FFFFFF",
    "card_alt": "#F8FAFC",
    "border": "#E2E8F0",
    "text": "#132033",
    "muted": "#64748B",
    "accent": "#3B82F6",
    "accent_hover": "#2563EB",
    "accent_soft": "#EAF2FF",
    "accent_soft_border": "#CFE0FF",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#EF4444",
    "on_accent": "#FFFFFF",
}

DARK = {
    "bg": "#080D18",
    "card": "#111827",
    "card_alt": "#172033",
    "border": "#263248",
    "text": "#F8FAFC",
    "muted": "#93A4BC",
    "accent": "#3B82F6",
    "accent_hover": "#2563EB",
    "accent_soft": "#0D2245",
    "accent_soft_border": "#1E4B86",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#EF4444",
    "on_accent": "#FFFFFF",
}

APPEARANCE_LABELS = {"System": "跟随系统", "Light": "浅色", "Dark": "深色"}
APPEARANCE_MODES = {value: key for key, value in APPEARANCE_LABELS.items()}


def system_prefers_dark():
    """系统是否处于深色模式。"""
    hints = getattr(QApplication, "styleHints", None)
    if hints is not None:
        try:
            scheme = QApplication.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return True
            if scheme == Qt.ColorScheme.Light:
                return False
        except Exception:  # 老版本 Qt 没有 colorScheme
            logger.debug("colorScheme 不可用，回退到注册表", exc_info=True)

    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return not bool(value)
        except Exception:
            logger.debug("读取应用主题失败，按浅色处理", exc_info=True)
    return False


def resolve_palette(mode="System"):
    """把 System/Light/Dark 解析成具体调色板。"""
    if mode == "Dark":
        return dict(DARK)
    if mode == "Light":
        return dict(LIGHT)
    return dict(DARK if system_prefers_dark() else LIGHT)


def build_qss(p):
    """生成全局 QSS。用 px 而不是 pt，Qt 会按 DPI 缩放逻辑像素。"""
    return f"""
    QWidget {{
        font-family: "{FONT_FAMILY}", {FONT_FALLBACK};
        font-size: 13px;
        color: {p['text']};
    }}
    QMainWindow, QDialog {{ background: {p['bg']}; }}
    #Page {{ background: {p['bg']}; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}

    QFrame#Card {{
        background: {p['card']};
        border: 1px solid {p['border']};
        border-radius: 14px;
    }}
    QFrame#Hero {{
        background: {p['accent_soft']};
        border: 1px solid {p['accent_soft_border']};
        border-radius: 14px;
    }}
    QFrame#Header {{
        background: {p['card']};
        border: none;
        border-bottom: 1px solid {p['border']};
    }}
    QFrame#Preference {{
        background: {p['card_alt']};
        border: none;
        border-radius: 10px;
    }}

    QLabel {{ background: transparent; }}
    QLabel#Title {{ font-size: 20px; font-weight: 600; }}
    QLabel#HeroTitle {{ font-size: 17px; font-weight: 600; }}
    QLabel#Subtitle {{ font-size: 12px; color: {p['muted']}; }}
    QLabel#SectionTitle {{ font-size: 15px; font-weight: 600; }}
    QLabel#FieldLabel {{ font-size: 11px; color: {p['muted']}; }}
    QLabel#Hint {{ font-size: 11px; color: {p['muted']}; }}
    QLabel#Badge {{
        color: {p['on_accent']};
        font-size: 11px;
        font-weight: 600;
        padding: 3px 12px;
        border-radius: 10px;
    }}
    QLabel#AppMark {{
        background: {p['accent']};
        color: #FFFFFF;
        font-size: 16px;
        font-weight: 600;
        border-radius: 12px;
    }}
    QLabel#CardMark {{
        background: {p['card_alt']};
        color: {p['accent']};
        font-size: 15px;
        font-weight: 600;
        border-radius: 10px;
    }}

    QPushButton {{
        background: {p['card_alt']};
        color: {p['text']};
        border: 1px solid {p['border']};
        border-radius: 8px;
        padding: 8px 16px;
        font-size: 12px;
    }}
    QPushButton:hover {{ border-color: {p['accent']}; }}
    QPushButton:pressed {{ background: {p['border']}; }}
    QPushButton:disabled {{ color: {p['muted']}; border-color: {p['border']}; }}
    QPushButton#Primary {{
        background: {p['accent']};
        color: #FFFFFF;
        border: none;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {p['accent_hover']}; }}
    QPushButton#Primary:disabled {{ background: {p['muted']}; color: {p['card']}; }}

    QPushButton#Segment {{
        background: transparent;
        border: none;
        border-radius: 7px;
        padding: 6px 14px;
        color: {p['muted']};
    }}
    QPushButton#Segment:hover {{ color: {p['text']}; }}
    QPushButton#Segment:checked {{
        background: {p['accent']};
        color: #FFFFFF;
        font-weight: 600;
    }}
    QFrame#SegmentBar {{
        background: {p['card_alt']};
        border: 1px solid {p['border']};
        border-radius: 9px;
    }}

    QLineEdit {{
        background: {p['card_alt']};
        border: 1px solid {p['border']};
        border-radius: 8px;
        padding: 9px 12px;
        font-size: 13px;
        selection-background-color: {p['accent']};
        selection-color: #FFFFFF;
    }}
    QLineEdit:focus {{ border: 1px solid {p['accent']}; }}
    QLineEdit::placeholder {{ color: {p['muted']}; }}

    QProgressBar {{
        background: {p['card_alt']};
        border: none;
        border-radius: 6px;
        height: 12px;
        text-align: center;
        font-size: 11px;
        color: {p['muted']};
    }}
    QProgressBar::chunk {{
        background: {p['accent']};
        border-radius: 6px;
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {p['border']};
        border-radius: 5px;
        min-height: 32px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QMenu {{
        background: {p['card']};
        border: 1px solid {p['border']};
        border-radius: 8px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 7px 22px 7px 14px;
        border-radius: 6px;
        font-size: 12px;
    }}
    QMenu::item:selected {{ background: {p['accent']}; color: #FFFFFF; }}
    QMenu::item:disabled {{ color: {p['muted']}; }}
    QMenu::separator {{
        height: 1px;
        background: {p['border']};
        margin: 5px 8px;
    }}
    """


def apply_theme(app, mode="System"):
    """应用全局样式，返回当前调色板。"""
    palette = resolve_palette(mode)
    app.setStyleSheet(build_qss(palette))
    return palette


def repolish(widget):
    """动态属性变化后刷新样式（QSS 不会自动重算选择器）。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_badge(label, text, background, foreground="#FFFFFF"):
    """圆角状态徽章：改文字 + 底色。"""
    label.setText(text)
    label.setStyleSheet(
        f"background: {background}; color: {foreground};"
        " font-size: 11px; font-weight: 600; padding: 3px 12px; border-radius: 10px;"
    )


def app_icon(state="active"):
    """任务栏 / 窗口图标，跟随系统浅深主题。"""
    path = icon_path(state)
    return QIcon(str(path)) if path is not None else QIcon()


class ToggleSwitch(QWidget):
    """iOS 风格开关：圆角轨道 + 带缓动的圆点。"""

    toggled = Signal(bool)

    def __init__(self, parent=None, checked=False, palette=None):
        super().__init__(parent)
        self._checked = bool(checked)
        self._offset = 1.0 if self._checked else 0.0
        self._palette = palette or LIGHT
        self.setFixedSize(46, 26)
        self.setCursor(Qt.PointingHandCursor)
        self._animation = QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def sizeHint(self):
        return QSize(46, 26)

    # --- offset 属性驱动动画 ---
    def get_offset(self):
        return self._offset

    def set_offset(self, value):
        self._offset = float(value)
        self.update()

    offset = Property(float, get_offset, set_offset)

    def isChecked(self):
        return self._checked

    def setChecked(self, checked, animate=True):
        """程序化设置状态；不发出 toggled 信号，避免回环。"""
        checked = bool(checked)
        if checked == self._checked:
            self._sync(animate)
            return
        self._checked = checked
        self._sync(animate)

    def _sync(self, animate=True):
        target = 1.0 if self._checked else 0.0
        self._animation.stop()
        if animate and self.isVisible():
            self._animation.setStartValue(self._offset)
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self.set_offset(target)

    def setPaletteColors(self, palette):
        self._palette = palette
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self._checked = not self._checked
            self._sync(True)
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        p = self._palette
        track = QRectF(0, 0, self.width(), self.height())
        radius = track.height() / 2

        if not self.isEnabled():
            track_color = QColor(p["border"])
            knob_color = QColor(p["card_alt"])
        elif self._checked:
            track_color = QColor(p["accent"])
            knob_color = QColor("#FFFFFF")
        else:
            track_color = QColor(p["border"])
            knob_color = QColor(p["card"])

        painter.setPen(Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, radius, radius)

        diameter = self.height() - 6
        travel = self.width() - diameter - 6
        x = 3 + travel * self._offset
        painter.setBrush(knob_color)
        painter.setPen(QPen(QColor(0, 0, 0, 28), 1))
        painter.drawEllipse(QRectF(x, 3, diameter, diameter))
        painter.end()
