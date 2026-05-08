#!/usr/bin/env python3
"""
qt5_viewer_gfx.py - PyQt5 RDP viewer using the rdpgfx pipeline + PyAV decoder.

What this demonstrates:
  - Everything qt5_viewer_legacy.py does, PLUS:
  - GraphicsPipelineChannel attached so the server uses MS-RDPEGFX
  - on_surface_bits callback that hands H.264 frames to PyAV (libav)
  - Decoded frames painted into a QImage just like the legacy path
  - Custom channel (PYECHO) bidirectional messaging shown in a status panel

What you trade for the speed:
  - Extra runtime dep: PyAV (`pip install av`). PyAV bundles ffmpeg.
  - Modern-only: GFX requires a server that supports it (Win8+/Server 2012+).
  - We only decode H.264. RemoteFX-Progressive and AVC444 paths are
    surfaced but flagged "decoder not configured."

Run-time deps: PyQt5, av (PyAV)
    pip install PyQt5 av

Usage:
    python examples/qt5_viewer_gfx.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import av    # PyAV
    HAVE_AV = True
except ImportError:
    HAVE_AV = False

from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPainter
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QStatusBar, QTextEdit, QVBoxLayout, QWidget,
)

from qt5_common import ConnectionDialog, InputBridge, RdpWorker

from pyfreerdp import (
    ClipboardChannel, CustomChannel, DisplayControlChannel,
    DriveRedirection, DriveRedirectionChannel,
    GfxCodec, GraphicsPipelineChannel, RdpClient, RdpSettings,
)


# ---------------------------------------------------------------------------
# Viewport with H.264 decoder
# ---------------------------------------------------------------------------

class GfxViewport(QWidget):
    """
    QWidget that decodes H.264 NAL units from rdpgfx and blits the
    resulting frames into a backbuffer QImage.

    PyAV handles annex-B framing detection, so we feed each SurfaceBits
    payload directly into a CodecContext.parse + CodecContext.decode loop.
    Frames come out as YUV; we convert to BGRA via av.VideoFrame.reformat.
    """

    update_received = pyqtSignal()

    def __init__(self, width=1920, height=1080):
        super(GfxViewport, self).__init__()
        self._image = QImage(width, height, QImage.Format_RGB32)
        self._image.fill(0xFF202020)
        self.setMinimumSize(QSize(640, 480))

        self._decoder = None
        if HAVE_AV:
            try:
                # Codec context for H.264 decode. We feed it raw NAL units
                # via parse() -> decode().
                self._decoder = av.CodecContext.create("h264", "r")
            except Exception as e:
                sys.stderr.write(
                    "Couldn't create H.264 decoder: {0}\n".format(e))
                self._decoder = None

    def desktop_size(self):
        return self._image.width(), self._image.height()

    def has_decoder(self):
        return self._decoder is not None

    def feed_h264_payload(self, x, y, w, h, payload):
        """
        Push a surface-bits H.264 payload through PyAV. May produce 0..N
        frames per call. Each frame is composited at (x, y).
        """
        if self._decoder is None:
            return 0
        decoded_count = 0
        try:
            packets = self._decoder.parse(payload)
            for pkt in packets:
                for frame in self._decoder.decode(pkt):
                    self._composite_frame(x, y, w, h, frame)
                    decoded_count += 1
        except Exception as e:
            sys.stderr.write("H.264 decode error: {0}\n".format(e))
        if decoded_count:
            self.update_received.emit()
            self.update()
        return decoded_count

    def _composite_frame(self, x, y, w, h, frame):
        # PyAV gives us a VideoFrame; reformat to BGRA and read bytes.
        # PyAV's `bgra` format produces 4-byte little-endian B,G,R,A which
        # matches Qt's Format_RGB32 in memory (B G R X on little-endian).
        bgra = frame.reformat(width=w, height=h, format="bgra")
        raw = bytes(bgra.planes[0])
        # PyAV may pad rows; use frame's reported linesize.
        stride = bgra.planes[0].line_size

        src = QImage(raw, w, h, stride, QImage.Format_RGB32).copy()
        painter = QPainter(self._image)
        painter.drawImage(x, y, src)
        painter.end()

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawImage(0, 0, self._image)
        p.end()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    surface_bits_arrived = pyqtSignal(object)
    pyecho_received = pyqtSignal(bytes)

    def __init__(self):
        super(MainWindow, self).__init__()
        self.setWindowTitle("pyfreerdp PyQt5 viewer (rdpgfx + H.264)")

        self._client = None
        self._worker = None
        self._clipboard = None
        self._disp = None
        self._gfx = None
        self._pyecho = None
        self._drive_share_dir = None

        # Layout: viewport on left, debug/log + custom channel UI on right.
        self._viewport = GfxViewport(1920, 1080)

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.addWidget(QLabel("Custom channel (PYECHO):"))

        self._echo_input = QLineEdit()
        self._echo_input.setPlaceholderText("Type and Send to remote...")
        self._echo_send_btn = QPushButton("Send")
        echo_row = QHBoxLayout()
        echo_row.addWidget(self._echo_input)
        echo_row.addWidget(self._echo_send_btn)
        side_layout.addLayout(echo_row)
        self._echo_send_btn.clicked.connect(self._send_echo)

        side_layout.addWidget(QLabel("Log:"))
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        side_layout.addWidget(self._log)
        side.setMaximumWidth(360)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self._viewport, stretch=1)
        main_layout.addWidget(side)
        central = QWidget()
        central.setLayout(main_layout)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self._update_status()

        self.surface_bits_arrived.connect(self._on_surface_bits_gui)
        self.pyecho_received.connect(self._on_pyecho_gui)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._send_disp_resize)

    def _update_status(self):
        if not HAVE_AV:
            self.statusBar().showMessage(
                "WARNING: PyAV not installed - H.264 frames will be "
                "dropped. Run: pip install av")
        elif self._viewport.has_decoder():
            self.statusBar().showMessage("Disconnected (H.264 decoder ready)")
        else:
            self.statusBar().showMessage(
                "Disconnected (decoder init failed)")

    def _log_line(self, text):
        self._log.append(text)

    # ---------------------------------------------------------- connect flow

    def show_connect_dialog(self):
        dlg = ConnectionDialog(self)
        if dlg.exec_() != dlg.Accepted:
            QApplication.instance().quit()
            return
        v = dlg.values()
        if not v["host"]:
            return self.show_connect_dialog()
        self._begin_connection(v)

    def _begin_connection(self, v):
        channels = []

        if v.pop("_use_clipboard"):
            self._clipboard = ClipboardChannel()
            channels.append(self._clipboard)
        if v.pop("_use_disp"):
            self._disp = DisplayControlChannel()
            channels.append(self._disp)

        # Drive share.
        share_dir = tempfile.mkdtemp(prefix="pyfreerdp-share-")
        with open(os.path.join(share_dir, "README.txt"), "w") as fh:
            fh.write("Local share via PyQt5 GFX viewer.\n")
        self._drive_share_dir = share_dir
        channels.append(DriveRedirectionChannel(drives=[
            DriveRedirection(name="pyshare", local_path=share_dir),
        ]))

        # *** The GFX channel ***
        # Subclass GraphicsPipelineChannel so on_frame routes here.
        # The base class drops by default; we forward to the worker hook.
        self._gfx = _GfxToWorker(self._on_surface_bits_worker)
        channels.append(self._gfx)

        # Custom channel demo - shows up on the right panel.
        self._pyecho = CustomChannel(
            name=b"PYECHO",
            on_data=lambda buf, flags: self.pyecho_received.emit(buf),
            dynamic=False,
        )
        channels.append(self._pyecho)

        settings = RdpSettings(
            host=v["host"], port=v["port"],
            username=v["username"], password=v["password"],
            domain=v["domain"],
            ignore_certificate=v["ignore_certificate"],
            width=self._viewport.desktop_size()[0],
            height=self._viewport.desktop_size()[1],
            color_depth=32,
            # GFX explicitly enabled. The server will prefer H.264 if
            # the cap exchange agrees; otherwise progressive RemoteFX.
            gfx_codec=GfxCodec.H264,
            gfx_h264=True,
            gfx_avc444=False,
            channels=channels,
        )

        self._client = RdpClient(settings)
        # Hook surface_bits, NOT bitmap_update. With GFX enabled the
        # bitmap path is mostly idle.
        self._client.on_surface_bits = self._on_surface_bits_worker_native

        InputBridge(self._client, self._viewport).install()

        self._worker = RdpWorker(self._client)
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.error.connect(self._on_error)
        self._worker.start()
        self.statusBar().showMessage(
            "Connecting to {0}:{1}...".format(v["host"], v["port"]))

    # --------------------------------------------- worker-thread callbacks

    def _on_surface_bits_worker_native(self, sbc):
        """RdpClient.on_surface_bits delivers SurfaceBits from worker thread."""
        # Marshal to GUI thread via signal.
        self.surface_bits_arrived.emit(sbc)

    def _on_surface_bits_worker(self, sbc):
        # Used when feeding via the channel-class subclass; same path.
        self.surface_bits_arrived.emit(sbc)

    def _on_surface_bits_gui(self, sbc):
        """Process a SurfaceBits event on the GUI thread."""
        if sbc.codec_name == "h264":
            n = self._viewport.feed_h264_payload(
                sbc.x, sbc.y, sbc.width, sbc.height, sbc.payload)
            if n == 0:
                # First frames often need an SPS/PPS - PyAV's parser
                # will buffer until it has a complete frame. Logging
                # nothing on n==0 keeps the log clean.
                return
        elif sbc.codec_name == "uncompressed":
            # Raw BGRA - paint directly. SurfaceBits' uncompressed
            # payload follows the negotiated pixel_format.
            src = QImage(sbc.payload, sbc.width, sbc.height,
                         sbc.width * 4, QImage.Format_RGB32).copy()
            painter = QPainter(self._viewport._image)
            painter.drawImage(sbc.x, sbc.y, src)
            painter.end()
            self._viewport.update()
        else:
            # remotefx-progressive, avc444, planar, jpeg, nscodec - all
            # need their own decoder. Log once-per-codec to avoid spam.
            seen = getattr(self, "_logged_codecs", set())
            if sbc.codec_name not in seen:
                seen.add(sbc.codec_name)
                self._logged_codecs = seen
                self._log_line(
                    "[gfx] codec {0!r} not handled in this example "
                    "({1} byte payload at frame {2})".format(
                        sbc.codec_name, len(sbc.payload), sbc.frame_id))

    def _on_pyecho_gui(self, payload):
        try:
            text = payload.decode("utf-8", "replace")
        except Exception:
            text = repr(payload)
        self._log_line("PYECHO recv: {0}".format(text))

    def _send_echo(self):
        if not self._pyecho or not self._client or not self._client.is_connected:
            self._log_line("(can't send: not connected)")
            return
        text = self._echo_input.text()
        if not text:
            return
        try:
            self._pyecho.send(text.encode("utf-8"))
            self._log_line("PYECHO send: {0}".format(text))
            self._echo_input.clear()
        except Exception as e:
            self._log_line("PYECHO send error: {0}".format(e))

    def _on_connected(self):
        self.statusBar().showMessage("Connected (rdpgfx active)")
        self._log_line("[connect] handshake complete")

    def _on_disconnected(self):
        self.statusBar().showMessage("Disconnected")
        self._log_line("[connect] session ended")

    def _on_error(self, msg):
        self._log_line("[error] {0}".format(msg))
        QMessageBox.critical(self, "Connection error", msg)

    # ----------------------------------------------------------- resize

    def resizeEvent(self, event):
        super(MainWindow, self).resizeEvent(event)
        self._resize_timer.start(300)

    def _send_disp_resize(self):
        if not self._disp or not self._client or not self._client.is_connected:
            return
        s = self._viewport.size()
        w, h = s.width(), s.height()
        if w < 200 or h < 200:
            return
        try:
            if self._disp.send_resize(w, h):
                self._log_line("[disp] resize -> {0}x{1}".format(w, h))
        except Exception as e:
            self._log_line("[disp] resize failed: {0}".format(e))

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
        if self._drive_share_dir:
            try:
                import shutil
                shutil.rmtree(self._drive_share_dir, ignore_errors=True)
            except Exception:
                pass
        super(MainWindow, self).closeEvent(event)


# A tiny GraphicsPipelineChannel subclass that just forwards on_frame
# to the embedder. RdpClient's surface-bits callback covers most of
# what we need; this is here to demonstrate the alternative entrypoint.
class _GfxToWorker(GraphicsPipelineChannel):

    def __init__(self, on_frame_cb):
        super(_GfxToWorker, self).__init__(prefer_h264=True)
        self._cb = on_frame_cb

    def on_frame(self, codec, payload, surface_id, x, y, w, h):
        # The base class' on_frame is a no-op; this version routes to
        # the worker hook. Note: in the current binding revision the
        # actual delivery path is RdpClient.on_surface_bits (more
        # general), so this method may not fire until the channel
        # plugin's frame-extraction layer is wired up. Kept for
        # forward compat.
        self._cb(payload)


def main():
    if not HAVE_AV:
        sys.stderr.write(
            "WARNING: PyAV not installed. H.264 frames from rdpgfx will "
            "be dropped silently. To install:\n"
            "    pip install av\n"
            "Continuing anyway so you can see the rest of the channels work.\n")
    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(1920 + 360, 1080)
    win.show()
    QTimer.singleShot(0, win.show_connect_dialog)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
