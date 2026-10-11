import json
import unittest
from types import SimpleNamespace

from app.auto_select import apply_dns_rules_to_profiles, build_auto_select_config
from app.models import DnsRule


def dns_rule(name, config, include_tags='', exclude_tags='', priority=100):
    return DnsRule(
        name=name,
        dns_config_json=json.dumps(config),
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        priority=priority,
        is_active=True,
    )


class DnsResolutionTests(unittest.TestCase):
    def test_auto_select_and_individual_profiles_receive_independent_dns(self):
        gsettings = SimpleNamespace(
            auto_select_tag_name='AUTO SELECT',
            auto_select_ping_timeout_ms=2000,
            auto_select_ping_interval_sec=60,
            auto_select_history_checks=10,
            auto_select_min_uptime_pct=90,
        )
        uris = [
            'vless://user-1@one.example:443?type=tcp&security=none#EU%20Premium',
            'vless://user-2@two.example:443?type=tcp&security=none#US%20Standard',
        ]
        profiles, outbound_count = build_auto_select_config(uris, [], gsettings)
        rules = [
            dns_rule('Auto Select DNS', {'servers': ['8.8.8.8']}, 'AUTO SELECT'),
            dns_rule('EU DNS', {'servers': ['9.9.9.9']}, 'EU'),
        ]

        apply_dns_rules_to_profiles(profiles, rules)

        self.assertEqual(outbound_count, 2)
        self.assertEqual(profiles[0]['remarks'], 'AUTO SELECT')
        self.assertEqual(profiles[0]['dns'], {'servers': ['8.8.8.8']})
        self.assertEqual(profiles[0]['routing']['rules'][0]['balancerTag'], 'auto-select')
        self.assertEqual(profiles[0]['routing']['rules'][0]['network'], 'tcp,udp')
        self.assertEqual(profiles[1]['dns'], {'servers': ['9.9.9.9']})
        self.assertNotIn('dns', profiles[2])

    def test_first_matching_rule_wins_and_exclude_tag_takes_precedence(self):
        profiles = [{'remarks': 'EU premium'}]
        rules = [
            dns_rule('Excluded high-priority rule', {'servers': ['8.8.8.8']}, 'EU', 'premium', 200),
            dns_rule('Lower-priority rule', {'servers': ['9.9.9.9']}, 'EU', priority=100),
        ]

        apply_dns_rules_to_profiles(profiles, rules)

        self.assertEqual(profiles[0]['dns'], {'servers': ['9.9.9.9']})


if __name__ == '__main__':
    unittest.main()
