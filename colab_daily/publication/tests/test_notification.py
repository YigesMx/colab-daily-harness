import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from colab_daily.publication.notification import notify
from colab_daily.storage import StorageError


class Config:
    def __init__(self, root):
        self.project_root = root
        self.storage_dir = root / "storage"


class FakeStore:
    def __init__(self, phase="Released"):
        self.phase = phase
        self.history = []
        self.started = 0
        self.results = []
        self.export = {
            "frozen_sha256": "a" * 64,
            "publication": {
                "display_date": "2026-01-02",
                "records": [
                    {"category": "Paper", "title": "Synthetic paper"},
                    {"category": "News", "title": "Synthetic news"},
                ],
            },
        }

    def export_publication(self, cycle):
        return self.export

    def delivery_history(self, cycle, channel):
        return self.history

    def start_delivery(self, cycle, channel, attempt, request):
        if self.phase != "Released":
            raise StorageError("notification requires formal Released publication")
        self.started += 1
        self.history = [{"attempt_id": attempt, "state": "pending", "request": request, "evidence": None}]

    def record_delivery(self, cycle, channel, attempt, state, evidence):
        self.results.append((state, evidence))
        self.history[0]["state"] = state
        self.history[0]["evidence"] = evidence


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name))
        self.env = {"FEISHU_WEBHOOK_URL": "https://hooks.example.org/robot", "COLAB_SITE_URL": "https://daily.example.org/"}

    def tearDown(self):
        self.temp.cleanup()

    def test_confirmed_delivery_writes_intent_then_acceptance(self):
        store = FakeStore()
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0}
        session = Mock()
        session.post.return_value = response
        self.assertEqual(notify(self.config, "daily-2026-01-02", store=store, session=session, environ=self.env), "confirmed")
        self.assertEqual(store.started, 1)
        self.assertEqual(store.results[0][0], "confirmed")
        self.assertTrue(store.results[0][1]["accepted"])
        payload = session.post.call_args.kwargs["json"]
        self.assertIn("已正式发布", payload["content"]["text"])

    def test_unknown_request_is_recorded_and_never_reposted(self):
        store = FakeStore()
        session = Mock()
        session.post.side_effect = requests.Timeout()
        with self.assertRaisesRegex(StorageError, "unknown"):
            notify(self.config, "daily-2026-01-02", store=store, session=session, environ=self.env)
        self.assertEqual(store.results[0][0], "unknown")
        second = Mock()
        with self.assertRaisesRegex(StorageError, "unknown"):
            notify(self.config, "daily-2026-01-02", store=store, session=second, environ=self.env)
        second.post.assert_not_called()

    def test_confirmed_retry_needs_no_live_webhook_configuration(self):
        store = FakeStore()
        store.history = [{"attempt_id": "released-report-v1", "state": "confirmed", "request": {}, "evidence": {}}]
        session = Mock()
        self.assertEqual(notify(self.config, "daily-2026-01-02", store=store, session=session, environ={}), "confirmed")
        session.post.assert_not_called()

    def test_existing_pending_intent_is_never_posted_again(self):
        store = FakeStore()
        store.history = [{"attempt_id": "released-report-v1", "state": "pending", "request": {}, "evidence": None}]
        session = Mock()
        with self.assertRaisesRegex(StorageError, "unknown or pending"):
            notify(self.config, "daily-2026-01-02", store=store, session=session, environ=self.env)
        session.post.assert_not_called()

    def test_notification_cannot_start_before_release(self):
        store = FakeStore(phase="PHASE_release")
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0}
        session = Mock()
        with self.assertRaisesRegex(StorageError, "Released"):
            notify(self.config, "daily-2026-01-02", store=store, session=session, environ=self.env)
        session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
