import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"

from app.models import Subscription, db
from app.sync_service import SyncService


class DiffPanel:
    class Config:
        name = "test-panel"

    config = Config()

    def __init__(self, client):
        self.client = client

    def get_inbounds(self):
        return [{"id": 1, "settings": json.dumps({"clients": [self.client]})}]


class ControllerUpdateSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import main

        cls.main = main
        cls.app = main.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()

    def test_repeating_expiry_days_recalculates_absolute_expiry(self):
        old_expiry = datetime.utcnow() + timedelta(hours=1)
        with self.app.app_context():
            subscription = Subscription(
                email="user@example.com",
                uuid="client-uuid",
                expiry_days=30,
                expire_at=old_expiry,
            )
            db.session.add(subscription)
            db.session.commit()
            subscription_id = subscription.id

        with (
            self.app.test_client() as client,
            patch.object(self.main.sync_service, "schedule_sync"),
        ):
            response = client.put(
                f"/api/subscriptions/{subscription_id}",
                json={"expiry_days": 30},
                headers={"Authorization": "Basic YWRtaW46YWRtaW4="},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        with self.app.app_context():
            updated = db.session.get(Subscription, subscription_id)
            self.assertGreater(updated.expire_at, old_expiry)

    def test_expiry_drift_is_reported_as_panel_update(self):
        with self.app.app_context():
            subscription = Subscription(
                email="drift@example.com",
                uuid="drift-client-uuid",
                expiry_days=30,
                expire_at=datetime.utcnow() + timedelta(days=30),
            )
            db.session.add(subscription)
            db.session.commit()

            expected = subscription.to_xui_client()
            panel = DiffPanel(
                {
                    "id": subscription.uuid,
                    "totalGB": expected["totalGB"],
                    "expiryTime": expected["expiryTime"] - 60_000,
                    "enable": expected["enable"],
                }
            )
            service = SyncService(object(), app=self.app)

            plan = service._calculate_panel_diff(
                panel,
                {subscription.uuid: subscription},
                {},
            )

        self.assertEqual(len(plan["update"]), 1)
        self.assertIn("expiryTime differs", plan["update"][0]["differences"])


if __name__ == "__main__":
    unittest.main()
