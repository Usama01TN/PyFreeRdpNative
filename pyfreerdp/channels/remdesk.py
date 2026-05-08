"""
remdesk - Remote Desktop Channel (MS-RA).

Supplemental channel used in Windows Remote Assistance for ticket exchange,
session-state events, and the "expert can take control" flow on top of
encomsp. Ships in every FreeRDP server-enabled build.

Direction: bidirectional.

If you're not building a Remote Assistance compatible client/server, you
don't need this channel. We register it so attach() doesn't fail when
it's listed in settings.channels alongside encomsp.
"""

from .base import ChannelSpec


class RemdeskChannel(ChannelSpec):
    """
    The remdesk static virtual channel.

    on_ticket_exchanged: callable(expert_id, novice_id) - invoked when the
                         ticket-redemption handshake completes.
    """

    NAME = b"remdesk"
    IS_DYNAMIC = False

    def __init__(self):
        super(RemdeskChannel, self).__init__()
        self.on_ticket_exchanged = None

    def params(self):
        return []

    def _dispatch_ticket(self, expert_id, novice_id):
        if self.on_ticket_exchanged:
            self.on_ticket_exchanged(expert_id, novice_id)
