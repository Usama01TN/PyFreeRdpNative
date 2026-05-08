"""
rail - Remote Application Integrated Locally (MS-RDPERP).

When a server is configured for RemoteApp (a.k.a. "RAIL"), the client
doesn't see a full desktop - it sees individual application windows that
appear to be running locally. The rail channel carries the window-state
sync, taskbar integration, system menu commands, etc.

Direction: bidirectional.

This module registers the channel and exposes hooks for the most common
events embedders care about: a window appears, a window is closed, the
window list changes. The full RAIL surface is large (it has 30+ PDU
types covering shell integration); this binding focuses on the subset
useful for "I'm building a RemoteApp viewer in Python".
"""

from .base import ChannelSpec, ChannelDirection


class RailChannel(ChannelSpec):
    """
    The rail static virtual channel.

    Construction:
      exec_app:      command line of the remote application to launch on
                     handshake completion. Required for client-side
                     RemoteApp use; for server-side, leave None.
      working_dir:   working directory on the remote.
      arguments:     extra command-line args appended to exec_app.
      compose_input: True if you handle composed unicode input client-side
                     (default True).
    """

    NAME = b"rail"
    IS_DYNAMIC = False
    DIRECTION = ChannelDirection.BOTH

    def __init__(self, exec_app=None, working_dir=None, arguments=None,
                 compose_input=True):
        super(RailChannel, self).__init__()
        self.exec_app = exec_app
        self.working_dir = working_dir
        self.arguments = arguments or ""
        self.compose_input = compose_input

        # Window-state callbacks. Fire from the channel data dispatcher.
        self.on_window_created = None       # fn(window_id, title)
        self.on_window_destroyed = None     # fn(window_id)
        self.on_window_title_changed = None # fn(window_id, new_title)
        self.on_window_state_changed = None # fn(window_id, state)
                                            # state: 'normal'/'min'/'max'

    def params(self):
        out = []
        if self.exec_app:
            out.append("exec:{0}".format(self.exec_app))
        if self.working_dir:
            out.append("workdir:{0}".format(self.working_dir))
        if self.arguments:
            out.append("args:{0}".format(self.arguments))
        if not self.compose_input:
            out.append("compose:0")
        return out

    # --- internal: invoked by channel dispatcher when window-state PDUs
    #     are decoded by FreeRDP's rail plugin ---

    def _dispatch_window_event(self, kind, window_id, **kwargs):
        if kind == "created" and self.on_window_created:
            self.on_window_created(window_id, kwargs.get("title", ""))
        elif kind == "destroyed" and self.on_window_destroyed:
            self.on_window_destroyed(window_id)
        elif kind == "title" and self.on_window_title_changed:
            self.on_window_title_changed(
                window_id, kwargs.get("title", ""))
        elif kind == "state" and self.on_window_state_changed:
            self.on_window_state_changed(
                window_id, kwargs.get("state", "normal"))
