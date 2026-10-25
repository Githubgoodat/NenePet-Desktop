"""No credentials or network: routing policy alone."""
import unittest

from aipet.ui.phone_router import PhoneNotificationRouter


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, value):
        for callback in self.callbacks:
            callback(value)


class Primary:
    def __init__(self, available):
        self.available = available
        self.received = []
        self.automatic_notice_failed = Signal()

    def can_send_automatic(self):
        return self.available

    def enqueue(self, notice):
        self.received.append(notice)


class Backup:
    def __init__(self):
        self.received = []

    def enqueue(self, notice):
        self.received.append(notice)


class PhoneRouterTests(unittest.TestCase):
    def make(self, available=True):
        primary, backup = Primary(available), Backup()
        return PhoneNotificationRouter(primary, backup), primary, backup

    def test_available_primary_is_used_without_parallel_duplicate(self):
        router, primary, backup = self.make()
        notice = object()
        router.enqueue(notice)
        self.assertEqual(primary.received, [notice])
        self.assertEqual(backup.received, [])

    def test_disabled_or_paused_primary_uses_backup_for_new_notice(self):
        router, primary, backup = self.make(False)
        notice = object()
        router.enqueue(notice)
        self.assertEqual(primary.received, [])
        self.assertEqual(backup.received, [notice])

    def test_explicit_failure_transfers_that_notice_and_future_notices_use_backup(self):
        router, primary, backup = self.make()
        first, later = object(), object()
        router.enqueue(first)
        primary.available = False
        primary.automatic_notice_failed.emit(first)
        router.enqueue(later)
        self.assertEqual(primary.received, [first])
        self.assertEqual(backup.received, [first, later])

    def test_shutdown_rejects_late_primary_failure_and_new_notices(self):
        router, primary, backup = self.make()
        router.shutdown()
        primary.automatic_notice_failed.emit(object())
        router.enqueue(object())
        self.assertEqual(primary.received, [])
        self.assertEqual(backup.received, [])


if __name__ == "__main__":
    unittest.main()
