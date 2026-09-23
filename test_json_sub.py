"""Тест JSON-массива подписки на реальной подписке."""
import base64
import sys
import urllib.request

sys.path.insert(0, '.')
from app.auto_select import build_auto_select_config

SUB_URL = 'https://avavakvn.dynnamn.ru/sub/MSUjP77BPSnMKGLmIvQvYg'

req = urllib.request.Request(SUB_URL, headers={'User-Agent': 'v2rayN/6.0'})
raw = urllib.request.urlopen(req, timeout=10).read().strip()
# Контроллер уже отдаёт JSON-массив (auto_select включён) — парсим как JSON
import json
try:
    profiles = json.loads(raw)
    print(f'Subscription is already JSON: {type(profiles)}')
    
    # Проверяем, является ли это профилем (списком) или полным конфигом
    if isinstance(profiles, list):
        print(f'Это массив профилей: {len(profiles)} профилей')
        if len(profiles) > 0:
            p0 = profiles[0]
            print(f'[0] remarks: {p0.get("remarks")}, outbounds: {len(p0.get("outbounds", []))}')
            for p in profiles[1:]:
                o = p['outbounds'][0]
                print(f'    - {p.get("remarks")} -> {o["settings"]["address"]}:{o["settings"]["port"]}')
        sys.exit(0)
    elif isinstance(profiles, dict):
        print(f'Это полный Xray конфиг (словарь)')
        print(f'Ключи: {list(profiles.keys())}')
        if 'outbounds' in profiles:
            print(f'Количество outbounds: {len(profiles["outbounds"])}')
            # Ищем авто-селект
            for i, outbound in enumerate(profiles['outbounds']):
                tag = outbound.get('tag', '')
                if 'auto' in tag.lower() or 'select' in tag.lower():
                    print(f'Найден авто-селект: {tag} (индекс {i})')
        sys.exit(0)
    else:
        print(f'Неизвестный тип JSON: {type(profiles)}')
        sys.exit(1)
except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
    print(f'JSON parse issue: {e!r}, treating as base64')
    pad = (-len(raw)) % 4
    uris = [l.strip() for l in base64.b64decode(raw + b'=' * pad).decode().splitlines() if l.strip()]
    print(f'Извлечено {len(uris)} URI из base64')
    print(f'Первый URI: {uris[0][:100]}...' if uris else 'Нет URI')


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