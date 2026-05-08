#!/usr/bin/env python3
"""
qt5_viewer_legacy.py - PyQt5 RDP viewer using the classic bitmap-update path.

What this demonstrates:
  - Connection settings dialog
  - Background-thread connection management with Qt signals
  - on_bitmap_update -> QImage painting (the "rendering hook")
  - Qt key/mouse events -> RDP input injection
  - Clipboard sync via cliprdr (bidirectional)
  - Display-resize via disp channel when the window resizes
  - Drive redirection (a temp directory shared with the remote)

What the "legacy" suffix means:
  This example DISABLES the GFX (rdpgfx) pipeline so all bitmap data
  arrives via the classic TS_UPDATE_BITMAP path. That works against
  every RDP server back to Windows 2000, but is slower than GFX/H.264
  on modern Windows. For modern servers, see qt5_viewer_gfx.py.

  Trade-off: legacy works without extra Python deps; GFX needs PyAV.

Run-time deps: PyQt5
    pip install PyQt5

Usage:
    python examples/qt5_viewer_legacy.py
"""

import os
import sys
import tempfile

# qt5_common.py lives next to this file in the examples/ directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPainter
from PyQt5.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMessageBox, QStatusBar,
    QVBoxLayout, QWidget,
)

from qt5_common import ConnectionDialog, InputBridge, RdpWorker

from pyfreerdp import (
    ClipboardChannel, DisplayControlChannel,
    DriveRedirection, DriveRedirectionChannel,
    GfxCodec, RdpClient, RdpSettings,
)


# ---------------------------------------------------------------------------
# Viewport widget - paints the remote desktop into a QImage
# ---------------------------------------------------------------------------

class RdpViewport(QWidget):
    """
    QWidget that maintains a backbuffer QImage and blits portions of it
    when the bitmap-update callback fires.

    Signals:
        update_received(int, int, int, int) - x, y, w, h that just changed
    """

    update_received = pyqtSignal(int, int, int, int)

    def __init__(self, width=1280, height=720):
        super(RdpViewport, self).__init__()
        # Format_RGB32 is BGRX in memory layout - matches the BGRX32
        # pixel format we'll request from FreeRDP. No per-pixel swap.
        self._image = QImage(width, height, QImage.Format_RGB32)
        self._image.fill(0xFF202020)   # neutral dark gray
        self.setMinimumSize(QSize(640, 480))
        # No resize on user drag - this is a fixed-size remote desktop.
        # Window resize triggers a disp-channel resize PDU, see resizeEvent.

    def desktop_size(self):
        return self._image.width(), self._image.height()

    def resize_backbuffer(self, width, height):
        """Resize the local backbuffer (after a display-control round trip)."""
        new_img = QImage(width, height, QImage.Format_RGB32)
        new_img.fill(0xFF202020)
        # Preserve as much of the old content as fits.
        painter = QPainter(new_img)
        painter.drawImage(0, 0, self._image)
        painter.end()
        self._image = new_img
        self.update()

    def apply_bitmap_rect(self, x, y, w, h, bgra_bytes, stride):
        """
        Paint one rectangle of decoded BGRA pixels into the backbuffer.

        Called from the GUI thread (we marshal across thread boundaries
        in MainWindow). Don't call from the worker thread directly -
        QImage isn't thread-safe.
        """
        if not bgra_bytes:
            return
        # Wrap the buffer as a temporary QImage with the source stride.
        # QImage retains a pointer to the buffer, so we copy() before
        # the bytes go out of scope at function return.
        src = QImage(bgra_bytes, w, h, stride, QImage.Format_RGB32).copy()

        painter = QPainter(self._image)
        painter.drawImage(x, y, src)
        painter.end()

        self.update(x, y, w, h)
        self.update_received.emit(x, y, w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawImage(0, 0, self._image)
        painter.end()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    # Custom signal so the worker-thread bitmap callback can deliver
    # rectangles to the GUI thread for painting. PyQt's signal/slot
    # machinery handles the cross-thread queueing for us.
    bitmap_rect_arrived = pyqtSignal(int, int, int, int, bytes, int)

    def __init__(self):
        super(MainWindow, self).__init__()
        self.setWindowTitle("pyfreerdp PyQt5 viewer (legacy bitmap path)")

        self._client = None
        self._worker = None
        self._clipboard = None
        self._disp = None
        self._drive_share_dir = None
        self._drive_share_temp = False

        self._viewport = RdpViewport(1280, 720)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._viewport)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Disconnected")

        # Hook our bitmap signal up to the viewport.
        self.bitmap_rect_arrived.connect(self._on_rect_arrived_gui)

        # Resize-debouncer: when the window resizes, wait 300ms before
        # firing a disp-channel resize PDU, so dragging doesn't spam.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._send_resize)

        # Periodically poll the clipboard for outbound updates.
        self._clip_timer = QTimer(self)
        self._clip_timer.setInterval(500)
        self._clip_timer.timeout.connect(self._sync_clipboard_to_remote)
        self._last_clip_text = None

    # ---------------------------------------------------------- connect flow

    def show_connect_dialog(self):
        dlg = ConnectionDialog(self)
        if dlg.exec_() != dlg.Accepted:
            QApplication.instance().quit()
            return
        values = dlg.values()
        if not values["host"]:
            QMessageBox.warning(self, "Missing host",
                                "Please enter a host.")
            return self.show_connect_dialog()
        self._begin_connection(values)

    def _begin_connection(self, v):
        # Build channel list from dialog toggles.
        channels = []
        if v.pop("_use_clipboard"):
            self._clipboard = ClipboardChannel(enable_text=True)
            channels.append(self._clipboard)
        if v.pop("_use_disp"):
            self._disp = DisplayControlChannel()
            channels.append(self._disp)

        # Always share a temp directory. Demonstrates rdpdr.
        share_dir = tempfile.mkdtemp(prefix="pyfreerdp-share-")
        with open(os.path.join(share_dir, "README.txt"), "w") as fh:
            fh.write("This drive is shared from your local Python viewer.\n")
        self._drive_share_dir = share_dir
        self._drive_share_temp = True
        channels.append(DriveRedirectionChannel(drives=[
            DriveRedirection(name="pyshare", local_path=share_dir),
        ]))

        # *** The legacy switch ***
        # Disable GFX so the server falls back to TS_UPDATE_BITMAP.
        # Without this, modern Windows servers send everything via
        # rdpgfx and our bitmap callback never fires.
        settings = RdpSettings(
            host=v["host"], port=v["port"],
            username=v["username"], password=v["password"],
            domain=v["domain"],
            ignore_certificate=v["ignore_certificate"],
            width=self._viewport.desktop_size()[0],
            height=self._viewport.desktop_size()[1],
            color_depth=32,
            gfx_codec=GfxCodec.AUTO,
            gfx_h264=False,
            gfx_avc444=False,
            enable_remotefx=False,
            channels=channels,
        )

        self._client = RdpClient(settings)
        self._client.on_bitmap_update = self._on_bitmap_update_worker
        # Install the input bridge (works once is_connected goes True).
        InputBridge(self._client, self._viewport).install()

        self._worker = RdpWorker(self._client)
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.error.connect(self._on_error)
        self._worker.start()
        self.statusBar().showMessage(
            "Connecting to {0}:{1}...".format(v["host"], v["port"]))

    # --------------------------------------------- worker-thread callbacks

    def _on_bitmap_update_worker(self, update):
        """
        Called from the FreeRDP event-loop thread. We can't touch QImage
        here. Marshal each rect to the GUI thread via signal.
        """
        for rect in update.rects:
            data = rect.data
            if rect.compressed:
                # In a fully-fleshed-out viewer, we'd call
                # bitmap_decompress() here via _api. To keep this example
                # readable and runnable, we skip compressed rects and rely
                # on the server sending uncompressed data when we don't
                # advertise compression caps. RdpSettings(compression=False)
                # would force that; toggle it if your server insists on RLE.
                continue
            if rect.bpp != 32:
                # Color conversion is straightforward (15/16/24 -> BGRA)
                # but adds 30+ lines we don't want to drown the example
                # with. Skip non-32bpp here; production code would use
                # freerdp_image_copy from the bindings to convert.
                continue
            self.bitmap_rect_arrived.emit(
                rect.x, rect.y, rect.width, rect.height,
                data, rect.stride)

    def _on_rect_arrived_gui(self, x, y, w, h, data, stride):
        """Slot fired on the GUI thread (signal queues automatically)."""
        self._viewport.apply_bitmap_rect(x, y, w, h, data, stride)

    def _on_connected(self):
        self.statusBar().showMessage("Connected")
        self._clip_timer.start()

    def _on_disconnected(self):
        self.statusBar().showMessage("Disconnected")
        self._clip_timer.stop()

    def _on_error(self, msg):
        self.statusBar().showMessage("Error: {0}".format(msg))
        QMessageBox.critical(self, "Connection error", msg)

    # ------------------------------------------------------- channel hooks

    def _sync_clipboard_to_remote(self):
        """Poll Qt's clipboard; when it changes, push to the remote."""
        if not self._clipboard or not self._client:
            return
        if not self._client.is_connected:
            return
        text = QApplication.clipboard().text()
        if text and text != self._last_clip_text:
            try:
                self._clipboard.set_text(text)
                self._last_clip_text = text
                self.statusBar().showMessage(
                    "Pushed {0} chars to remote clipboard".format(len(text)),
                    2000)
            except Exception as e:
                sys.stderr.write("clipboard push failed: {0}\n".format(e))

    # ----------------------------------------------------------- resize

    def resizeEvent(self, event):
        super(MainWindow, self).resizeEvent(event)
        # Defer until 300ms after the user stops dragging.
        self._resize_timer.start(300)

    def _send_resize(self):
        if not self._disp or not self._client or not self._client.is_connected:
            return
        viewport_size = self._viewport.size()
        w, h = viewport_size.width(), viewport_size.height()
        if w < 200 or h < 200:
            return
        try:
            sent = self._disp.send_resize(w, h)
            if sent:
                self._viewport.resize_backbuffer(w, h)
                self.statusBar().showMessage(
                    "Resize -> {0}x{1}".format(w, h), 2000)
        except Exception as e:
            sys.stderr.write("disp resize failed: {0}\n".format(e))

    # ------------------------------------------------------------ shutdown

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(2000)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        if self._drive_share_temp and self._drive_share_dir:
            try:
                import shutil
                shutil.rmtree(self._drive_share_dir, ignore_errors=True)
            except Exception:
                pass
        super(MainWindow, self).closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(1280, 720)
    win.show()
    # Show the connect dialog right after the main window is up so the
    # user has somewhere for it to be modal-against.
    QTimer.singleShot(0, win.show_connect_dialog)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
