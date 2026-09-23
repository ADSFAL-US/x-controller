"""Генератор JSON-подписки (Xray) с автовыбором для Happ/Incognito/v2rayTun.

Формат — JSON-массив профилей (Happ "JSON Arrays"):
  [0] — автоселект-профиль: полный Xray конфиг с балансером и observatory.
       Все конфиги пользователя внутри как аутбаунды, настоящий автовыбор
       с fallback-цепочкой по tier-ам. remarks = имя тега из настроек WUI.
  [1..n] — каждый конфиг пользователя отдельным мини-профилем
       (один аутбаунд + socks inbound), remarks = имя конфига.

Каждый элемент передаётся в Xray core 1:1 (включая VLESS Encryption
mlkem768x25519plus), так что все конфиги работают как обычно.

Параметры берутся из GlobalSettings (таймауты пинга, sampling) и
AutoSelectRule (fallback-уровни по include/exclude тегам).
"""

import json
import urllib.parse

AUTO_SELECT_TAG_PREFIX = 'as-'
DEFAULT_PROBE_URL = 'https://connectivitycheck.gstatic.com/generate_204'


XHTTP_FLAT_FIELDS = (
    'path', 'host', 'mode',
    'scMaxConcurrentPosts', 'scMaxEachPostBytes', 'scMinPostsIntervalMs',
)

XHTTP_EXTRA_FIELDS = (
    'scMaxBufferedPosts',
    'scMaxEachPostBytes', 'scMinPostsIntervalMs',   # дубликаты flat, НЕ сливать!
    'uplinkHTTPMethod', 'noSSEHeader', 'noGRPCHeader',
    'xPaddingBytes', 'xPaddingHeader', 'xPaddingKey',
    'xPaddingMethod', 'xPaddingObfsMode', 'xPaddingPlacement',
)


def _coerce_param(raw):
    """'10' -> 10, 'true' -> True, '0-0' -> '0-0' (строка)."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw


def parse_vless_link(link, tag):
    """Парсит vless:// URI в Xray outbound JSON (совместимо с 3x-ui)."""
    try:
        part = link.split('://', 1)[1]
        body = part.split('#', 1)[0]
        userinfo, rest = body.split('@', 1)
        netloc, query_str = rest.split('?', 1)
        address, port = netloc.rsplit(':', 1)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query_str).items()}

        # ── user ──────────────────────────────────────────────
        user = {'id': userinfo, 'security': 'auto'}
        enc = q.get('encryption')
        if enc and enc != 'none':
            user['encryption'] = enc
        flow = q.get('flow')
        if flow:
            user['flow'] = flow

        # ── streamSettings ────────────────────────────────────
        stream = {}
        network = q.get('type', 'tcp')
        stream['network'] = network

        if network == 'grpc':
            grpc = {'serviceName': q.get('serviceName', '')}
            if q.get('mode') == 'multi':
                grpc['multiMode'] = True
            stream['grpcSettings'] = grpc

        elif network == 'ws':
            ws = {}
            if q.get('path'):
                ws['path'] = q['path']
            if q.get('host'):
                ws['headers'] = {'Host': q['host']}
            stream['wsSettings'] = ws

        elif network in ('xhttp', 'splithttp'):
            xhttp = {}

            # ПЛОСКИЕ поля верхнего уровня xhttpSettings
            for key in XHTTP_FLAT_FIELDS:
                if key in q:
                    xhttp[key] = _coerce_param(q[key])

            # ВЛОЖЕННЫЙ extra — только из явного extra= или из legacy-flat
            extra = {}
            extra_str = q.get('extra')
            if extra_str:
                try:
                    parsed_extra = json.loads(extra_str)
                    if isinstance(parsed_extra, dict):
                        extra.update(parsed_extra)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Если продюсер (трансформер) записал extra-поля плоско — подберём.
            # ВАЖНО: НЕ трогаем scMaxEachPostBytes/scMinPostsIntervalMs, они уже
            # ушли flat выше. Если хочется продублировать их в extra — бери
            # из q['extra']-JSON, а не из плоских query.
            for key in XHTTP_EXTRA_FIELDS:
                if key in ('scMaxEachPostBytes', 'scMinPostsIntervalMs'):
                    continue  # уже flat
                if key in q and key not in extra:
                    extra[key] = _coerce_param(q[key])

            if extra:
                xhttp['extra'] = extra
            if xhttp:
                stream['xhttpSettings'] = xhttp

        elif network == 'httpupgrade':
            hu = {}
            if q.get('path'):
                hu['path'] = q['path']
            if q.get('host'):
                hu['host'] = q['host']
            stream['httpupgradeSettings'] = hu

        # security / reality / tls — как было
        security = q.get('security', '')
        if security == 'reality':
            stream['security'] = 'reality'
            reality = {
                'serverName': q.get('sni', ''),
                'fingerprint': q.get('fp', 'chrome'),
                'publicKey': q.get('pbk', ''),
                'shortId': q.get('sid', ''),
            }
            if q.get('spx'):
                reality['spiderX'] = q['spx']
            stream['realitySettings'] = reality
        elif security == 'tls':
            stream['security'] = 'tls'
            tls = {}
            if q.get('sni'):
                tls['serverName'] = q['sni']
            if q.get('alpn'):
                tls['alpn'] = q['alpn'].split(',')
            if q.get('fp'):
                tls['fingerprint'] = q['fp']
            if q.get('allowInsecure') in ('1', 'true', 'True'):
                tls['allowInsecure'] = True
            if tls:
                stream['tlsSettings'] = tls

        # ── outbound ──────────────────────────────────────────
        return {
            'tag': tag,
            'protocol': 'vless',
            'settings': {
                'vnext': [{
                    'address': address,
                    'port': int(port),
                    'users': [user],
                }]
            },
            'streamSettings': stream,
        }
    except Exception:
        return None


def _uri_name(uri):
    """Имя конфига (fragment после #), URL-декодированное."""
    if '#' not in uri:
        return ''
    try:
        return urllib.parse.unquote(uri.split('#', 1)[1])
    except Exception:
        return uri.split('#', 1)[1]


def _matches_tier(name, rule):
    """Проверяет имя конфига против include/exclude тегов правила."""
    if not name:
        return False
    name_lower = name.lower()
    include = [t.lower() for t in rule.get_include_tags()]
    exclude = [t.lower() for t in rule.get_exclude_tags()]
    if exclude and any(t in name_lower for t in exclude):
        return False
    return not (include and not any(t in name_lower for t in include))


def _build_single_profile(uri, remark):
    """Мини-профиль для одного конфига: один аутбаунд + socks inbound."""
    out = parse_vless_link(uri, 'proxy')
    if not out:
        return None
    return {
        'log': {'loglevel': 'warning'},
        'remarks': remark,
        'inbounds': [
            {
                'tag': 'in',
                'listen': '127.0.0.1',
                'port': 10808,
                'protocol': 'socks',
                'settings': {'udp': True},
                'sniffing': {'enabled': True, 'routeOnly': True},
            }
        ],
        'outbounds': [out, {'tag': 'direct', 'protocol': 'freedom'}],
    }


def build_auto_select_config(all_uris, rules, gsettings):
    """Собирает JSON-массив подписки: автоселект-профиль + все конфиги отдельно.

    :param all_uris: список vless:// URI пользователя (уже отфильтрованный preset'ом)
    :param rules: список AutoSelectRule (active), отсортированный по priority DESC
    :param gsettings: GlobalSettings
    :return: (list профилей, int число конфигов в автовыборе) или (None, 0)
    """
    if not all_uris:
        return None, 0

    # Имя автоселект-профиля из настроек WUI
    tag_name = (getattr(gsettings, 'auto_select_tag_name', None) or '⚡ AUTO SELECT').strip() or '⚡ AUTO SELECT'

    # Раскладываем URI по tiers согласно правилам
    tiers = [[] for _ in rules] + [[]]  # последний — "всё остальное"
    used = set()
    for uri in all_uris:
        name = _uri_name(uri)
        for i, rule in enumerate(rules):
            if uri not in used and _matches_tier(name, rule):
                tiers[i].append(uri)
                used.add(uri)
                break
    # Финальный tier: всё, что не попало в правила
    tiers[-1] = [u for u in all_uris if u not in used]

    # Строим аутбаунды с тегами as-t<tier>-<idx>
    outbounds = []
    tier_tags = []  # список тегов по tier-ам (для costs)
    for tier_idx, tier_uris in enumerate(tiers):
        tags = []
        for idx, uri in enumerate(tier_uris):
            tag = f"{AUTO_SELECT_TAG_PREFIX}t{tier_idx}-{idx}"
            out = parse_vless_link(uri, tag)
            if out:
                outbounds.append(out)
                tags.append(tag)
        tier_tags.append(tags)

    # Хотя бы 2 живых кандидата для осмысленного автовыбора
    if len(outbounds) < 2:
        return None, len(outbounds)

    # costs: чем ниже приоритет tier-а, тем выше cost (менее предпочтителен).
    # RTTDeviationCost = RTT * sqrt(cost), так что cost 1 / 9 / 25 ...
    # даёт чёткое ранжирование tier-ов при живых верхних.
    costs = []
    for tier_idx, tags in enumerate(tier_tags):
        if not tags:
            continue
        cost_value = (tier_idx + 1) ** 2  # 1, 4, 9, 16...
        costs.append({
            'regexp': False,
            'match': f"{AUTO_SELECT_TAG_PREFIX}t{tier_idx}-",
            'value': cost_value,
        })

    # fallbackTag: первый аутбаунд самого приоритетного непустого tier-а
    fallback_tag = None
    for tags in tier_tags:
        if tags:
            fallback_tag = tags[0]
            break

    # Параметры пинга из GlobalSettings
    timeout_ms = int(gsettings.auto_select_ping_timeout_ms or 2000)
    interval_sec = max(10, int(gsettings.auto_select_ping_interval_sec or 60))
    sampling = max(2, int(gsettings.auto_select_history_checks or 10))
    # tolerance leastLoad: максимально допустимая доля неудачных проб
    min_uptime = int(gsettings.auto_select_min_uptime_pct or 90)
    tolerance = max(0.0, min(1.0, (100 - min_uptime) / 100.0))

    config = {
        'log': {'loglevel': 'warning'},
        # Имя профиля в списке клиента (Happ: remarks)
        'remarks': tag_name,
        'inbounds': [
            {
                'tag': 'in',
                'listen': '127.0.0.1',
                'port': 10808,
                'protocol': 'socks',
                'settings': {'udp': True},
                'sniffing': {'enabled': True, 'routeOnly': True},
            }
        ],
        'outbounds': outbounds + [
            {'tag': 'direct', 'protocol': 'freedom'},
            {'tag': 'block', 'protocol': 'blackhole'},
        ],
        'burstObservatory': {
            'subjectSelector': [AUTO_SELECT_TAG_PREFIX],
            'pingConfig': {
                'destination': DEFAULT_PROBE_URL,
                'connectivity': '',
                'interval': f"{interval_sec}s",
                'sampling': sampling,
                'timeout': f"{timeout_ms}ms",
                'httpMethod': 'HEAD',
            },
        },
        'routing': {
            'rules': [
                {'network': 'tcp,udp', 'balancerTag': 'auto-select'},
            ],
            'balancers': [
                {
                    'tag': 'auto-select',
                    'selector': [AUTO_SELECT_TAG_PREFIX],
                    'strategy': {
                        'type': 'leastLoad',
                        'settings': {
                            'expected': 1,
                            'tolerance': tolerance,
                            'costs': costs,
                        },
                    },
                    'fallbackTag': fallback_tag,
                },
            ],
        },
    }

    # JSON-массив подписки: [0] автоселект, [1..n] каждый конфиг отдельно
    profiles = [config]
    for uri in all_uris:
        profile = _build_single_profile(uri, _uri_name(uri))
        if profile:
            profiles.append(profile)

    return profiles, len(outbounds)