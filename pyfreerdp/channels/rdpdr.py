"""
rdpdr - Device Redirection Virtual Channel (MS-RDPEFS).

The rdpdr channel multiplexes several sub-protocols:
  * RDPDR_DTYP_FILESYSTEM (drives) -- redirect a local directory as a
    Windows drive on the remote
  * RDPDR_DTYP_PRINT     (printers)
  * RDPDR_DTYP_PORT      (serial / parallel)
  * RDPDR_DTYP_SMARTCARD (smartcards)

This module implements the FILESYSTEM sub-protocol via FreeRDP's built-in
`drive` channel module. The other three are stubs that raise on use - they
have working FreeRDP modules but require platform-specific glue (CUPS for
printers, PCSC for smartcards) that we don't pull into the binding by
default. To enable them, build FreeRDP with -DWITH_CUPS=ON / -DWITH_PCSC=ON
and use the `printer` / `smartcard` channel names directly via CustomChannel.

Drive redirection usage:

    drive = DriveRedirection(name="share", local_path="/home/me/shared")
    settings.channels = [DriveRedirectionChannel(drives=[drive])]

The remote sees a drive named "share" backed by /home/me/shared. Reads,
writes, lock/unlock, queries, etc. all flow through FreeRDP's `drive`
plugin which talks to the local filesystem via standard POSIX/Win32 calls.
"""

import os

from .base import ChannelSpec, ChannelOpenError


class DriveRedirection(object):
    """
    One redirected directory.

    name:        Drive name shown on the remote (max 8 chars; FreeRDP will
                 truncate if longer). Appears as ``\\\\TSCLIENT\\<name>``
                 on the remote.
    local_path:  Absolute path to a local directory the remote may
                 read/write through the redirection.
    read_only:   If True, FreeRDP's drive module rejects write operations.
                 Default False.
    """

    def __init__(self, name, local_path, read_only=False):
        if not name:
            raise ValueError("DriveRedirection.name is required")
        if not local_path:
            raise ValueError("DriveRedirection.local_path is required")
        # Don't enforce existence here - the path may not exist yet at
        # ChannelSpec construction time on the server side. FreeRDP's
        # drive module will surface the error at first access.
        if not os.path.isabs(local_path):
            raise ValueError(
                "local_path must be absolute: {0!r}".format(local_path))
        if len(name) > 8:
            # The RDP spec allows up to 8; FreeRDP silently truncates,
            # which is surprising. Reject early.
            raise ValueError(
                "drive name {0!r} > 8 chars (RDP limit). Pick a shorter "
                "name.".format(name))
        self.name = name
        self.local_path = local_path
        self.read_only = read_only


class DriveRedirectionChannel(ChannelSpec):
    """
    The rdpdr static virtual channel, configured for drive redirection.

    drives: list of DriveRedirection records describing what to expose.
    """

    NAME = b"rdpdr"
    IS_DYNAMIC = False

    def __init__(self, drives=None):
        super(DriveRedirectionChannel, self).__init__()
        if not drives:
            raise ValueError(
                "DriveRedirectionChannel needs at least one DriveRedirection")
        for d in drives:
            if not isinstance(d, DriveRedirection):
                raise TypeError(
                    "drives must be list of DriveRedirection, got {0}".format(
                        type(d).__name__))
        self.drives = list(drives)

    def params(self):
        # FreeRDP's rdpdr channel takes per-device sub-channel descriptors
        # of the form "drive,<name>,<path>,<rw|ro>". The whole list goes
        # to freerdp_client_add_static_channel as a parameter array.
        out = []
        for d in self.drives:
            mode = "ro" if d.read_only else "rw"
            out.append("drive,{0},{1},{2}".format(
                d.name, d.local_path, mode))
        return out

    def __repr__(self):
        return "DriveRedirectionChannel(drives={0})".format(
            [d.name for d in self.drives])
