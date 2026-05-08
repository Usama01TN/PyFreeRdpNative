"""
encomsp - Encompassing Multiparty Channel (MS-RDPEMC).

Used in remote-assistance scenarios where a session has multiple
participants (the "expert" and the "novice", typically). Carries:
  * participant list updates (who's connected)
  * role announcements (control owner vs viewer)
  * grant/revoke control PDUs

Direction: bidirectional.

Most embedders don't need this. It's included because it's part of the
standard channel set in any FreeRDP build with -DWITH_SERVER=ON.
"""

from .base import ChannelSpec


class EncompChannel(ChannelSpec):
    """
    The encomsp static virtual channel.

    on_participant_changed: callable(participant_list) - invoked when the
                            roster updates. participant_list is a list of
                            dicts with keys 'id', 'name', 'role'.
    on_control_changed:     callable(participant_id) - invoked when control
                            ownership changes.
    """

    NAME = b"encomsp"
    IS_DYNAMIC = False

    def __init__(self):
        super(EncompChannel, self).__init__()
        self.on_participant_changed = None
        self.on_control_changed = None

    def params(self):
        return []

    def _dispatch_participant(self, participants):
        if self.on_participant_changed:
            self.on_participant_changed(list(participants))

    def _dispatch_control(self, participant_id):
        if self.on_control_changed:
            self.on_control_changed(participant_id)
