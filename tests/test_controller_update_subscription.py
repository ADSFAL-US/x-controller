import os
import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"

from app.models import DnsRule, GlobalSettings, Subscription, db
from app.sync_service import SyncService


class DiffPanel:
    class Config:
        name = "test-panel"

    config = Config()

    def __init__(self, client):
        self.client = client

    def get_inbounds(self):
        return [{"id": 1, "settings": json.dumps({"clients": [self.client]})}]


class SubscriptionPanel:
    class Config:
        name = "dns-test-panel"

    config = Config()

    def login(self):
        return True

    def get_inbounds(self):
        return [{
            "id": 1,
            "port": 443,
            "protocol": "vless",
            "settings": json.dumps({
                "clients": [{
                    "id": "client-uuid",
                    "email": "dns@example.com",
                    "subId": "sub-id",
                }],
            }),
        }]

    def get_client_traffic(self, email):
        return {"up": 0, "down": 0}

    def get_subscription_content(self, sub_id):
        uris = "\n".join([
            "vless://client-uuid@one.example:443?type=tcp&security=none#EU%20Premium",
            "vless://client-uuid@two.example:443?type=tcp&security=none#US%20Standard",
        ])
        return base64.b64encode(uris.encode()).decode()


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

    def test_subscription_json_has_profile_dns_and_happ_flag(self):
        with self.app.app_context():
            db.session.add(GlobalSettings(
                auto_select_enabled=True,
                happ_dns_from_json_enabled=True,
                auto_select_tag_name="AUTO SELECT",
            ))
            db.session.add_all([
                DnsRule(
                    name="Auto Select DNS",
                    include_tags="AUTO SELECT",
                    dns_config_json='{"servers":["8.8.8.8"]}',
                    priority=100,
                ),
                DnsRule(
                    name="EU DNS",
                    include_tags="EU",
                    dns_config_json='{"servers":["9.9.9.9"]}',
                    priority=100,
                ),
            ])
            subscription = Subscription(
                email="dns@example.com",
                uuid="client-uuid",
                sub_token="dns-sub-token",
            )
            db.session.add(subscription)
            db.session.commit()

        with (
            self.app.test_client() as client,
            patch.object(self.main.xui_client, "panels", [SubscriptionPanel()]),
        ):
            response = client.get("/sub/dns-sub-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["dns-from-json-enable"], "true")
        self.assertEqual(response.mimetype, "application/json")
        profiles = response.get_json()
        self.assertEqual(profiles[0]["dns"], {"servers": ["8.8.8.8"]})
        self.assertEqual(profiles[0]["routing"]["rules"][0]["balancerTag"], "auto-select")
        self.assertEqual(profiles[1]["dns"], {"servers": ["9.9.9.9"]})
        self.assertNotIn("dns", profiles[2])

    def test_dns_rule_api_rejects_invalid_json_configuration(self):
        auth = {"Authorization": "Basic YWRtaW46YWRtaW4="}
        with self.app.test_client() as client:
            response = client.post(
                "/api/dns-rules",
                headers=auth,
                json={
                    "name": "Broken DNS",
                    "dns_config_json": "{invalid",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("valid JSON", response.json["error"])

    def test_dns_rule_api_creates_json_object_with_zero_priority(self):
        auth = {"Authorization": "Basic YWRtaW46YWRtaW4="}
        with self.app.test_client() as client:
            response = client.post(
                "/api/dns-rules",
                headers=auth,
                json={
                    "name": "DNS test",
                    "include_tags": "EU",
                    "dns_config_json": {"servers": ["1.1.1.1"]},
                    "priority": 0,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["rule"]["priority"], 0)
        self.assertEqual(
            json.loads(response.json["rule"]["dns_config_json"]),
            {"servers": ["1.1.1.1"]},
        )

    def test_legacy_base64_subscription_format_is_preserved(self):
        with self.app.app_context():
            db.session.add(GlobalSettings(
                auto_select_enabled=False,
                happ_dns_from_json_enabled=True,
            ))
            db.session.add(DnsRule(
                name="Legacy DNS rule",
                include_tags="EU",
                dns_config_json='{"servers":["9.9.9.9"]}',
            ))
            db.session.add(Subscription(
                email="legacy@example.com",
                uuid="client-uuid",
                sub_token="legacy-sub-token",
            ))
            db.session.commit()

        with (
            self.app.test_client() as client,
            patch.object(self.main.xui_client, "panels", [SubscriptionPanel()]),
        ):
            response = client.get("/sub/legacy-sub-token")

        decoded = base64.b64decode(response.get_data(as_text=True)).decode()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertIn("#EU%20Premium", decoded)
        self.assertEqual(response.headers["dns-from-json-enable"], "true")


if __name__ == "__main__":
    unittest.main()
