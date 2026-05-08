"""
qt5_common.py - shared scaffolding for both PyQt5 RDP viewer examples.

Provides:
  - A connection-settings dialog (host, user, password, channels)
  - A Qt-event -> RDP-input bridge (mouse, keyboard, scancode mapping)
  - A worker thread that pumps the RdpClient event loop
  - Signals plumbing for thread-safe update delivery

Run-time deps: PyQt5 only. Install with:
    pip install PyQt5

Style: Py2-compatible syntax. Tested with PyQt5 5.15+ on Python 3.8-3.12.
"""

import sys
import threading

try:
    from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal
    from PyQt5.QtGui import QImage, QKeyEvent
    from PyQt5.QtWidgets import (
        QApplication, QDialog, QDialogButtonBox, QFormLayout,
        QLineEdit, QCheckBox, QSpinBox,
    )
except ImportError:
    sys.stderr.write(
        "PyQt5 not installed. Install with: pip install PyQt5\n")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Connection-settings dialog
# ---------------------------------------------------------------------------

class ConnectionDialog(QDialog):
    """
    Modal dialog asking for host, username, password, port, and channel
    feature toggles. Accepted return: a dict ready to splat into
    RdpSettings(**kwargs).
    """

    def __init__(self, parent=None, default_host="", default_user=""):
        super(ConnectionDialog, self).__init__(parent)
        self.setWindowTitle("Connect to RDP Server")
        self.resize(420, 240)

        layout = QFormLayout(self)

        self.host_edit = QLineEdit(default_host)
        self.host_edit.setPlaceholderText("rdp.example.com")
        layout.addRow("Host:", self.host_edit)

        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(3389)
        layout.addRow("Port:", self.port_edit)

        self.user_edit = QLineEdit(default_user)
        layout.addRow("Username:", self.user_edit)

        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        layout.addRow("Password:", self.pass_edit)

        self.domain_edit = QLineEdit()
        self.domain_edit.setPlaceholderText("(optional)")
        layout.addRow("Domain:", self.domain_edit)

        self.clip_check = QCheckBox("Enable clipboard sync")
        self.clip_check.setChecked(True)
        layout.addRow(self.clip_check)

        self.disp_check = QCheckBox("Enable display-resize on window resize")
        self.disp_check.setChecked(True)
        layout.addRow(self.disp_check)

        self.ignore_cert = QCheckBox(
            "Ignore certificate errors (DEV ONLY)")
        self.ignore_cert.setChecked(True)
        layout.addRow(self.ignore_cert)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self):
        """Return a dict suitable for `RdpSettings(**values_minus_features)`."""
        return {
            "host": self.host_edit.text().strip(),
            "username": self.user_edit.text().strip(),
            "password": self.pass_edit.text(),
            "domain": self.domain_edit.text().strip(),
            "port": int(self.port_edit.value()),
            "ignore_certificate": bool(self.ignore_cert.isChecked()),
            "_use_clipboard": bool(self.clip_check.isChecked()),
            "_use_disp": bool(self.disp_check.isChecked()),
        }


# ---------------------------------------------------------------------------
# Input bridge - Qt key/mouse events -> RDP send_*
# ---------------------------------------------------------------------------

# Subset of Qt::Key -> RDP scancode (Microsoft RDP scancodes from
# include/freerdp/scancode.h). Full table is huge; this covers the
# most-needed keys. Embedders should extend.
QT_KEY_TO_SCANCODE = {
    Qt.Key_Escape: 0x01,
    Qt.Key_Tab: 0x0F,
    Qt.Key_Backspace: 0x0E,
    Qt.Key_Return: 0x1C,
    Qt.Key_Enter: 0x1C,
    Qt.Key_Space: 0x39,
    Qt.Key_Left: 0x4B,
    Qt.Key_Right: 0x4D,
    Qt.Key_Up: 0x48,
    Qt.Key_Down: 0x50,
    Qt.Key_Home: 0x47,
    Qt.Key_End: 0x4F,
    Qt.Key_PageUp: 0x49,
    Qt.Key_PageDown: 0x51,
    Qt.Key_Insert: 0x52,
    Qt.Key_Delete: 0x53,
    Qt.Key_Shift: 0x2A,
    Qt.Key_Control: 0x1D,
    Qt.Key_Alt: 0x38,
    Qt.Key_F1: 0x3B, Qt.Key_F2: 0x3C, Qt.Key_F3: 0x3D, Qt.Key_F4: 0x3E,
    Qt.Key_F5: 0x3F, Qt.Key_F6: 0x40, Qt.Key_F7: 0x41, Qt.Key_F8: 0x42,
    Qt.Key_F9: 0x43, Qt.Key_F10: 0x44, Qt.Key_F11: 0x57, Qt.Key_F12: 0x58,
}

# Keys that should be sent as "extended" scancodes (E0 prefix on the wire).
EXTENDED_KEYS = set([
    Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
    Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown,
    Qt.Key_Insert, Qt.Key_Delete,
])


def qt_button_to_rdp(button):
    """Map Qt.MouseButton enum to the strings RdpClient.send_mouse_button wants."""
    if button == Qt.LeftButton:
        return "left"
    if button == Qt.RightButton:
        return "right"
    if button == Qt.MiddleButton:
        return "middle"
    return None


class InputBridge(QObject):
    """
    Connect Qt key/mouse events to an RdpClient. Use as:

        bridge = InputBridge(client, viewport_widget)
        bridge.install()

    The bridge ignores events when client.is_connected is False.
    """

    def __init__(self, client, viewport):
        super(InputBridge, self).__init__()
        self._client = client
        self._viewport = viewport

    def install(self):
        # Qt requires the widget to accept focus to receive key events.
        self._viewport.setFocusPolicy(Qt.StrongFocus)
        self._viewport.setMouseTracking(True)
        # We don't subclass the widget; we attach event filters instead so
        # the user's QWidget subclass stays clean.
        self._viewport.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is not self._viewport:
            return False
        if not getattr(self._client, "is_connected", False):
            return False
        et = event.type()
        try:
            # KeyPress / KeyRelease
            from PyQt5.QtCore import QEvent
            if et == QEvent.KeyPress:
                self._on_key(event, pressed=True)
                return True
            if et == QEvent.KeyRelease:
                self._on_key(event, pressed=False)
                return True
            if et in (QEvent.MouseMove, QEvent.MouseButtonPress,
                      QEvent.MouseButtonRelease):
                self._on_mouse(event, et)
                return True
        except Exception as e:
            sys.stderr.write("InputBridge filter error: {0}\n".format(e))
        return False

    def _on_key(self, event, pressed):
        if not isinstance(event, QKeyEvent):
            return
        key = event.key()
        scancode = QT_KEY_TO_SCANCODE.get(key)
        if scancode is not None:
            extended = key in EXTENDED_KEYS
            self._client.send_key(scancode, pressed=pressed,
                                  extended=extended)
            return
        # Fallback: send as Unicode keystroke. Works for all printable
        # characters; the server does its own keymap translation.
        text = event.text()
        if text:
            for ch in text:
                self._client.send_unicode(ord(ch), pressed=pressed)

    def _on_mouse(self, event, et):
        from PyQt5.QtCore import QEvent
        x, y = int(event.x()), int(event.y())
        if et == QEvent.MouseMove:
            self._client.send_mouse_move(x, y)
            return
        btn = qt_button_to_rdp(event.button())
        if btn is None:
            return
        pressed = (et == QEvent.MouseButtonPress)
        self._client.send_mouse_button(btn, x, y, pressed=pressed)


# ---------------------------------------------------------------------------
# Event-loop worker thread
# ---------------------------------------------------------------------------

class RdpWorker(QThread):
    """
    Pumps RdpClient.run_event_loop in a background thread. Emits Qt
    signals for connection state so the UI thread can react.

    Usage:
        worker = RdpWorker(client)
        worker.connected.connect(on_connected)
        worker.disconnected.connect(on_disconnected)
        worker.error.connect(on_error)
        worker.start()
    """

    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, client):
        super(RdpWorker, self).__init__()
        self._client = client

    def run(self):
        try:
            self._client.connect()
            self.connected.emit()
            self._client.run_event_loop()
        except Exception as e:
            self.error.emit(str(e))
        finally:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self.disconnected.emit()

    def stop(self):
        try:
            self._client.stop()
        except Exception:
            pass


__all__ = [
    "QApplication",
    "ConnectionDialog",
    "InputBridge",
    "RdpWorker",
    "QT_KEY_TO_SCANCODE",
]
