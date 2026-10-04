import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"

from app.models import Subscription, db
from app.sync_service import SyncService
from app.xui_client import PanelConfig, XUIPanel


class ApiResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class ModernPanel:
    class Config:
        name = "modern-panel"
        legacy = False

    config = Config()

    def __init__(self, clients=None, inbounds=None):
        self.clients = copy.deepcopy(clients or [])
        self.inbounds = inbounds if inbounds is not None else [
            {"id": 1, "protocol": "vless", "settings": {}, "streamSettings": {}},
            {"id": 2, "protocol": "vless", "settings": {}, "streamSettings": {}},
        ]
        self.fail_client_read = False
        self.fail_attach = False

    def login(self):
        return True

    def get_inbounds(self):
        return self.inbounds

    def list_modern_clients(self):
        if self.fail_client_read:
            return None
        return copy.deepcopy(self.clients)

    def create_modern_client(self, client_data, inbound_ids):
        self.clients.append({"client": copy.deepcopy(client_data), "inbound_ids": list(inbound_ids)})
        return True

    def update_modern_client(self, email, client_data):
        for record in self.clients:
            if record["client"].get("email") == email:
                record["client"].update(copy.deepcopy(client_data))
                return True
        return False

    def attach_modern_client(self, email, inbound_ids):
        if self.fail_attach:
            return False
        for record in self.clients:
            if record["client"].get("email") == email:
                record["inbound_ids"] = sorted(set(record["inbound_ids"]) | set(inbound_ids))
                return True
        return False

    def detach_modern_client(self, email, inbound_ids):
        for record in self.clients:
            if record["client"].get("email") == email:
                record["inbound_ids"] = [
                    inbound_id for inbound_id in record["inbound_ids"]
                    if inbound_id not in inbound_ids
                ]
                return True
        return False

    def delete_modern_client(self, email):
        self.clients = [
            record for record in self.clients
            if record["client"].get("email") != email
        ]
        return True

    def add_client(self, *args, **kwargs):
        raise AssertionError("legacy add_client called for a modern panel")

    def update_client(self, *args, **kwargs):
        raise AssertionError("legacy update_client called for a modern panel")

    def delete_client(self, *args, **kwargs):
        raise AssertionError("legacy delete_client called for a modern panel")


class XuiModernAdapterTests(unittest.TestCase):
    def setUp(self):
        self.config = PanelConfig(
            name="modern-panel",
            host="https://panel.example",
            username="admin",
            password="secret",
            panel_path="/secret-path",
            legacy=False,
            api_token="panel-token",
        )
        self.panel = XUIPanel(self.config)

    def test_legacy_mode_is_safe_default_and_keeps_positional_priority(self):
        config = PanelConfig("legacy", "https://host", "user", "pass", 4)
        self.assertTrue(config.legacy)
        self.assertEqual(config.priority, 4)

    def test_modern_login_uses_bearer_token_without_password_login(self):
        with patch.object(self.panel.session, "get", return_value=ApiResponse({"success": True})) as get:
            self.assertTrue(self.panel.login())

        self.assertEqual(self.panel.session.headers["Authorization"], "Bearer panel-token")
        get.assert_called_once_with(
            "https://panel.example/secret-path/panel/api/inbounds/list", timeout=30
        )

    def test_modern_login_without_token_fails(self):
        self.config.api_token = ""
        with patch.object(self.panel.session, "get") as get:
            self.assertFalse(self.panel.login())
        get.assert_not_called()

    def test_modern_config_can_omit_username_and_password(self):
        config = PanelConfig(
            name="token-only",
            host="https://panel.example",
            legacy=False,
            api_token="panel-token",
        )
        self.assertEqual(config.username, "")
        self.assertEqual(config.password, "")

    def test_legacy_add_client_keeps_existing_endpoint_and_form_payload(self):
        config = PanelConfig("legacy", "https://panel.example", "admin", "secret")
        panel = XUIPanel(config)
        with patch.object(
            panel.session,
            "post",
            return_value=ApiResponse({"success": True}),
        ) as post:
            self.assertTrue(panel.add_client(3, {"id": "client-uuid"}))

        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://panel.example/panel/api/inbounds/addClient")
        self.assertEqual(kwargs["data"]["id"], 3)
        self.assertEqual(
            json.loads(kwargs["data"]["settings"]),
            {"clients": [{"id": "client-uuid"}]},
        )

    def test_modern_create_uses_clients_api_and_json_bindings(self):
        with patch.object(
            self.panel.session,
            "request",
            return_value=ApiResponse({"success": True}),
        ) as request:
            self.assertTrue(self.panel.create_modern_client({"email": "x"}, [3, 5]))

        request.assert_called_once_with(
            "POST",
            "https://panel.example/secret-path/panel/api/clients/add",
            timeout=30,
            json={"client": {"email": "x"}, "inboundIds": [3, 5]},
        )

    def test_modern_update_gets_and_preserves_full_client_payload(self):
        responses = [
            ApiResponse({
                "success": True,
                "obj": {
                    "client": {
                        "id": 73,
                        "uuid": "client-uuid",
                        "email": "stable-random-email",
                        "subId": "stable-sub-id",
                        "flow": "xtls-rprx-vision",
                        "password": "protocol-secret",
                        "totalGB": 10,
                    },
                    "inboundIds": [3],
                },
            }),
            ApiResponse({"success": True}),
        ]
        with patch.object(self.panel.session, "request", side_effect=responses) as request:
            self.assertTrue(self.panel.update_modern_client(
                "stable-random-email", {"totalGB": 20}
            ))

        posted = request.call_args_list[1].kwargs["json"]
        self.assertEqual(posted["id"], "client-uuid")
        self.assertEqual(posted["subId"], "stable-sub-id")
        self.assertEqual(posted["flow"], "xtls-rprx-vision")
        self.assertEqual(posted["password"], "protocol-secret")
        self.assertEqual(posted["totalGB"], 20)
        self.assertNotIn("uuid", posted)

    def test_client_list_normalizes_record_id_to_uuid(self):
        with patch.object(
            self.panel.session,
            "request",
            return_value=ApiResponse({
                "success": True,
                "obj": [{"id": 73, "uuid": "client-uuid", "email": "x", "inboundIds": [3]}],
            }),
        ):
            clients = self.panel.list_modern_clients()

        self.assertEqual(clients[0]["client"]["id"], "client-uuid")
        self.assertEqual(clients[0]["inbound_ids"], [3])


class ModernSubscriptionSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import main

        cls.app = main.app
        cls.app.config.update(TESTING=True)

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()

    def _make_subscription(self, enabled=True):
        subscription = Subscription(
            email="user@example.com",
            uuid="client-uuid",
            sub_id="client-uuid",
            enabled=enabled,
            total_gb=5,
        )
        db.session.add(subscription)
        db.session.commit()
        return subscription

    def _make_service(self, panel):
        return SyncService(SimpleNamespace(panels=[panel]), app=self.app)

    def test_create_makes_one_client_attached_to_every_inbound(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            panel = ModernPanel()
            result = self._make_service(panel).sync_subscription(subscription, "create")

        self.assertTrue(result["modern-panel"]["success"])
        self.assertEqual(len(panel.clients), 1)
        self.assertEqual(panel.clients[0]["inbound_ids"], [1, 2])
        self.assertEqual(panel.clients[0]["client"]["id"], "client-uuid")
        self.assertNotEqual(panel.clients[0]["client"]["email"], subscription.email)

    def test_modern_object_stream_settings_set_flow_and_numeric_tg_id(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            subscription.tg_id = "8675309"
            panel = ModernPanel(inbounds=[{
                "id": 3,
                "protocol": "vless",
                "settings": {"clients": []},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {"show": False},
                },
            }])
            result = self._make_service(panel).sync_subscription(subscription, "create")

        self.assertTrue(result["modern-panel"]["success"])
        self.assertEqual(panel.clients[0]["client"]["flow"], "xtls-rprx-vision")
        self.assertEqual(panel.clients[0]["client"]["tgId"], 8675309)

    def test_incompatible_modern_reality_flows_fail_without_creating(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            panel = ModernPanel(inbounds=[
                {
                    "id": 1,
                    "protocol": "vless",
                    "streamSettings": {
                        "network": "tcp",
                        "realitySettings": {"show": False},
                    },
                },
                {
                    "id": 2,
                    "protocol": "vless",
                    "streamSettings": {
                        "network": "xhttp",
                        "realitySettings": {"show": False},
                    },
                },
            ])
            result = self._make_service(panel).sync_subscription(subscription, "create")

        self.assertFalse(result["modern-panel"]["success"])
        self.assertIn("incompatible VLESS flow", result["modern-panel"]["error"])
        self.assertEqual(panel.clients, [])

    def test_update_reconciles_missing_and_extra_bindings(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            panel = ModernPanel(clients=[{
                "client": {
                    "id": "client-uuid",
                    "email": "panel-email",
                    "subId": "client-uuid",
                    "flow": "preserve-flow",
                    "totalGB": 1,
                    "expiryTime": 0,
                    "enable": True,
                },
                "inbound_ids": [1, 99],
            }])
            result = self._make_service(panel).sync_subscription(subscription, "update")

        self.assertTrue(result["modern-panel"]["success"])
        self.assertEqual(panel.clients[0]["inbound_ids"], [1, 2])
        self.assertEqual(panel.clients[0]["client"]["totalGB"], 5 * 1024 ** 3)
        self.assertEqual(panel.clients[0]["client"]["flow"], "preserve-flow")

    def test_existing_client_without_required_bind_is_drift(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            panel = ModernPanel(clients=[{
                "client": {"id": "client-uuid", "email": "panel-email"},
                "inbound_ids": [1],
            }])
            plan = self._make_service(panel)._calculate_panel_diff(
                panel, {subscription.uuid: subscription}, {}
            )

        self.assertEqual(len(plan["update"]), 1)
        self.assertIn("inbound bindings differ", plan["update"][0]["differences"])

    def test_modern_diff_deletes_disabled_subscription(self):
        with self.app.app_context():
            subscription = self._make_subscription(enabled=False)
            panel = ModernPanel(clients=[{
                "client": {"id": "client-uuid", "email": "panel-email"},
                "inbound_ids": [1, 2],
            }])
            plan = self._make_service(panel)._calculate_panel_diff(
                panel, {subscription.uuid: subscription}, {}
            )

        self.assertEqual(len(plan["delete"]), 1)
        self.assertEqual(plan["delete"][0]["email"], "panel-email")

    def test_client_read_failure_is_not_treated_as_absence_or_success(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            panel = ModernPanel()
            panel.fail_client_read = True
            result = self._make_service(panel).sync_subscription(subscription, "create")

        self.assertFalse(result["modern-panel"]["success"])
        self.assertIn("Failed to read clients", result["modern-panel"]["error"])
        self.assertEqual(panel.clients, [])

    def test_delete_uses_modern_email_api_even_without_inbounds(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            panel = ModernPanel(
                clients=[{
                    "client": {"id": "client-uuid", "email": "panel-email"},
                    "inbound_ids": [],
                }],
                inbounds=[],
            )
            result = self._make_service(panel).sync_subscription(subscription, "delete")

        self.assertTrue(result["modern-panel"]["success"])
        self.assertEqual(panel.clients, [])

    def test_bind_failure_marks_sync_failed(self):
        with self.app.app_context():
            subscription = self._make_subscription()
            panel = ModernPanel(clients=[{
                "client": {"id": "client-uuid", "email": "panel-email"},
                "inbound_ids": [1],
            }])
            panel.fail_attach = True
            result = self._make_service(panel).sync_subscription(subscription, "update")

        self.assertFalse(result["modern-panel"]["success"])
        self.assertIn("Failed to attach", result["modern-panel"]["error"])


if __name__ == "__main__":
    unittest.main()