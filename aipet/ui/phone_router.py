"""Route new metadata notices; never interpret messages as phone commands."""
from __future__ import annotations


class PhoneNotificationRouter:
    """Prefer WeChat, with opt-in Bark handling unavailable/explicit failures.

    The primary sender emits only definite failure facts for automatic notices.
    Ambiguous network or acceptance results are deliberately not replayed to a
    second channel. Each sender independently enforces DND, freshness and opt-in.
    """

    def __init__(self, weixin, bark):
        self.weixin = weixin
        self.bark = bark
        self._closed = False
        weixin.automatic_notice_failed.connect(self._fallback)

    def enqueue(self, notice):
        if self._closed:
            return
        if self.weixin.can_send_automatic():
            self.weixin.enqueue(notice)
        else:
            self.bark.enqueue(notice)

    def _fallback(self, notice):
        if not self._closed:
            self.bark.enqueue(notice)

    def shutdown(self):
        self._closed = True
