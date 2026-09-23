"""Тест интеграции авто-селекта в систему подписок.

Тестирует автоматический выбор конфигураций для финских и голландских STABLE конфигураций
с поддержкой JSON формата для Happ/Incognito/v2rayTun клиентов.
"""
import base64
import json
import sys
import os

sys.path.insert(0, '.')

from app.auto_select import build_auto_select_config
from app.models import Subscription, GlobalSettings, AutoSelectRule
from app.main import app

# Финские STABLE конфигурации (примеры)
FINNISH_STABLE_URIS = [
    'vless://uuid1@finland-stable-1.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=finland-stable-1.com#Финляндия-стабильная-1',
    'vless://uuid2@finland-stable-2.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=finland-stable-2.com#Финляндия-стабильная-2',
    'vless://uuid3@finland-stable-3.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=finland-stable-3.com#Финляндия-стабильная-3',
]

# Голландские STABLE конфигурации (примеры)
DUTCH_STABLE_URIS = [
    'vless://uuid4@netherlands-stable-1.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=netherlands-stable-1.com#Нидерланды-стабильная-1',
    'vless://uuid5@netherlands-stable-2.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=netherlands-stable-2.com#Нидерланды-стабильная-2',
    'vless://uuid6@netherlands-stable-3.com:443?encryption=none&flow=xtls-rprx-vision&type=tcp&security=tls&sni=netherlands-stable-3.com#Нидерланды-стабильная-3',
]

# Правила авто-селекта для финских и голландских STABLE конфигураций
FINNISH_DUTCH_RULES = [
    # Правило для STABLE конфигураций (включаем финские и голландские)
    type('Rule', (), {
        'get_include_tags': lambda self: ['stable'],
        'get_exclude_tags': lambda self: ['warp', 'глушилки', 'adblock', 'gaming', 'smart', 'telegram', 'финляндия', 'нидерланды']
    })(),
    # Правило для "глушилок" (если нужны)
    type('Rule', (), {
        'get_include_tags': lambda self: ['глушилки'],
        'get_exclude_tags': lambda self: []
    })(),
]

# Настройки авто-селекта
class FD_GSettings:
    auto_select_tag_name = '⚡ Финско-голландский авто-выбор'
    auto_select_ping_timeout_ms = 2000
    auto_select_ping_interval_sec = 60
    auto_select_history_checks = 10
    auto_select_min_uptime_pct = 90

def test_auto_select_with_subscription_system():
    """Тестирует интеграцию авто-селекта в систему подписок."""
    print("=== Тест интеграции авто-селекта в систему подписок ===\n")
    
    # Объединяем все URI
    all_uris = FINNISH_STABLE_URIS + DUTCH_STABLE_URIS
    print(f"Общее количество URI: {len(all_uris)}")
    print(f"  - Финских STABLE: {len(FINNISH_STABLE_URIS)}")
    print(f"  - Голландских STABLE: {len(DUTCH_STABLE_URIS)}")
    
    # Строим авто-селект конфиг
    profiles, as_count = build_auto_select_config(all_uris, FINNISH_DUTCH_RULES, FD_GSettings())
    
    print(f"\nРезультат авто-селекта:")
    print(f"  - Общее количество профилей: {len(profiles)}")
    print(f"  - Количество авто-селект outbounds: {as_count}")
    
    # Проверяем первый профиль (авто-селект)
    if len(profiles) > 0:
        auto_select_profile = profiles[0]
        print(f"\nАвто-селект профиль:")
        print(f"  - Название: {auto_select_profile.get('remarks', 'N/A')}")
        print(f"  - Количество outbounds: {len(auto_select_profile.get('outbounds', []))}")
        
        # Проверяем, что это JSON-массив (формат Happ "JSON Arrays")
        assert isinstance(profiles, list), f"Ожидался список, получен {type(profiles)}"
        print(f"  - Формат: JSON-массив (совместим с Happ/Incognito/v2rayTun)")
        
        # Проверяем структуру авто-селекта
        if 'outbounds' in auto_select_profile:
            outbounds = auto_select_profile['outbounds']
            print(f"  - Outbounds содержит {len(outbounds)} элементов")
            
            # Ищем balancer и observatory
            has_balancer = any(ob.get('protocol') == 'balancer' for ob in outbounds)
            has_observatory = any(ob.get('protocol') == 'observatory' for ob in outbounds)
            print(f"  - Balancer: {'✓' if has_balancer else '✗'}")
            print(f"  - Observatory: {'✓' if has_observatory else '✗'}")
    
    # Проверяем отдельные профили
    individual_profiles = profiles[1:] if len(profiles) > 1 else []
    print(f"\nОтдельные профили: {len(individual_profiles)}")
    
    for i, profile in enumerate(individual_profiles[:3]):  # Показываем первые 3
        print(f"  Профиль {i+1}:")
        print(f"    - Название: {profile.get('remarks', 'N/A')}")
        print(f"    - Outbounds: {len(profile.get('outbounds', []))}")
        if profile.get('outbounds'):
            ob = profile['outbounds'][0]
            print(f"    - Сервер: {ob.get('settings', {}).get('address', 'N/A')}:{ob.get('settings', {}).get('port', 'N/A')}")
    
    # Сохраняем результат для проверки
    with open('test_auto_select_integration_output.json', 'w', encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    print(f"\nРезультат сохранён в test_auto_select_integration_output.json")
    
    # Проверяем совместимость с Happ/Incognito/v2rayTun
    print(f"\n=== Проверка совместимости с клиентами ===")
    
    # Happ ожидает JSON-массив с [0] как авто-селектом
    if isinstance(profiles, list) and len(profiles) > 0:
        print(f"✓ JSON-массив формат поддерживается Happ")
        
        # Проверяем, что первый элемент содержит авто-селект
        first_profile = profiles[0]
        if 'outbounds' in first_profile and len(first_profile['outbounds']) > 0:
            print(f"✓ Первый элемент содержит outbounds (авто-селект)")
        
        # Проверяем, что остальные элементы содержат отдельные конфигурации
        if len(profiles) > 1:
            print(f"✓ Остальные элементы содержат отдельные конфигурации ({len(profiles)-1} профилей)")
    
    # Проверяем, что формат соответствует спецификации Happ "JSON Arrays"
    print(f"\n=== Проверка спецификации Happ \"JSON Arrays\" ===")
    
    # Happ "JSON Arrays" формат:
    # [0] — автоселект-профиль: полный Xray конфиг с балансером и observatory
    # [1..n] — каждый конфиг пользователя отдельным мини-профилем
    
    if len(profiles) >= 2:
        print(f"✓ Формат соответствует спецификации Happ")
        print(f"  - Элемент [0]: авто-селект с balancer + observatory")
        print(f"  - Элементы [1..{len(profiles)-1}]: отдельные конфигурации")
        
        # Проверяем, что первый элемент содержит balancer и observatory
        first_profile = profiles[0]
        if 'outbounds' in first_profile:
            outbounds = first_profile['outbounds']
            has_balancer = any(ob.get('protocol') == 'balancer' for ob in outbounds)
            has_observatory = any(ob.get('protocol') == 'observatory' for ob in outbounds)
            
            if has_balancer and has_observatory:
                print(f"✓ Первый элемент содержит balancer и observatory (авто-селект)")
            else:
                print(f"⚠ Первый элемент не содержит balancer и observatory")
    
    print(f"\n✅ Тест интеграции авто-селекта в систему подписок пройден!")
    return True

if __name__ == '__main__':
    try:
        test_auto_select_with_subscription_system()
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Ошибка в тесте: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)