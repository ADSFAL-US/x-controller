"""Простой тест авто-селекта"""
import base64
import sys
import json

sys.path.insert(0, '.')
from app.auto_select import build_auto_select_config

# Простой тест с базовым URI
uris = [
    'vless://uuid1@server1.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=server1.com',
    'vless://uuid2@server2.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=server2.com',
]

class Rule:
    def __init__(self, include, exclude):
        self._inc = include
        self._exc = exclude

    def get_include_tags(self):
        return self._inc

    def get_exclude_tags(self):
        return self._exc

class GSettings:
    auto_select_tag_name = '⚡ АВТОВЫБОР'
    auto_select_ping_timeout_ms = 2000
    auto_select_ping_interval_sec = 60
    auto_select_history_checks = 10
    auto_select_min_uptime_pct = 90

rules = [
    Rule(['stable'], ['warp', 'глушилки', 'adblock', 'gaming', 'smart', 'telegram']),
    Rule(['глушилки'], []),
]

profiles, n = build_auto_select_config(uris, rules, GSettings())
print(f'profiles: {len(profiles)}, auto-select outbounds: {n}')
print(f'[0] remarks: {profiles[0]["remarks"]}, outbounds: {len(profiles[0]["outbounds"])}')
for p in profiles[1:]:
    print(f'    - {p["remarks"]} -> {p["outbounds"][0]["settings"]["address"]}:{p["outbounds"][0]["settings"]["port"]}')

# Сохраняем результат для проверки
with open('test_simple_output.json', 'w', encoding='utf-8') as f:
    json.dump(profiles, f, ensure_ascii=False, indent=2)
print(f'\nРезультат сохранён в test_simple_output.json')

# Проверяем, что это JSON-массив
assert isinstance(profiles, list), f'Ожидался список, получен {type(profiles)}'
assert len(profiles) > 0, 'Пустой список профилей'
assert 'remarks' in profiles[0], 'Первый профиль должен иметь remarks'
assert 'outbounds' in profiles[0], 'Первый профиль должен иметь outbounds'

print('\n✅ Тест пройден!')