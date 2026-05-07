"""Клиент для работы с 3x-ui API.

Схема API из исходников: https://github.com/MHSanaei/3x-ui
"""

import json
import requests
import logging
import threading
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PanelConfig:
    """Конфигурация панели."""
    name: str
    host: str  # Базовый хост для API панели, например https://panel.example.com:2053
    username: str
    password: str
    priority: int = 1
    max_clients: int = 100
    panel_path: str = ''  # Путь к панели API, например /secret-path
    sub_host: str = ''  # Отдельный хост для подписок (если отличается от host)
    sub_path: str = '/sub'  # Путь к подписке


class XUIPanel:
    """Клиент для одной панели 3x-ui."""
    
    def __init__(self, config: PanelConfig):
        self.config = config
        # Thread-local storage for sessions - each thread gets its own Session
        self._thread_local = threading.local()
    
    def _get_session(self) -> requests.Session:
        """Get thread-local Session. Creates new session if needed."""
        if not hasattr(self._thread_local, 'session'):
            self._thread_local.session = requests.Session()
            self._thread_local.session.headers.update({
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            })
        return self._thread_local.session
    
    @property
    def session(self) -> requests.Session:
        """Property to access thread-local session."""
        return self._get_session()
    
    def login(self) -> bool:
        """
        Авторизоваться в панели 3x-ui.
        POST {panel_path}/login
        """
        try:
            url = f"{self.config.host}{self.config.panel_path}/login"
            data = {
                "username": self.config.username,
                "password": self.config.password
            }
            response = self.session.post(url, data=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    logger.info(f"Успешная авторизация в панели {self.config.name}")
                    return True
                else:
                    logger.error(f"Ошибка авторизации: {result}")
            else:
                logger.error(f"HTTP ошибка {response.status_code}")
            
            return False
        except Exception as e:
            logger.error(f"Ошибка подключения к панели {self.config.name}: {e}")
            return False
    
    def get_inbounds(self) -> List[Dict[str, Any]]:
        """
        Получить список inbounds через GET {panel_path}/panel/api/inbounds/list.
        """
        try:
            url = f"{self.config.host}{self.config.panel_path}/panel/api/inbounds/list"
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    return result.get("obj", [])
                else:
                    logger.warning(f"API вернул success=false: {result}")
            else:
                logger.error(f"HTTP ошибка {response.status_code}: {response.text[:200]}")
            
            return []
        except Exception as e:
            logger.error(f"Ошибка получения inbounds: {e}")
            return []
    
    def add_client(self, inbound_id: int, client_data: Dict[str, Any]) -> bool:
        """
        Добавить клиента к inbound.
        POST {panel_path}/panel/api/inbounds/addClient
        
        Формат данных:
        - id: inbound ID (int)
        - settings: JSON-строка с объектом {"clients": [...]}
        """
        try:
            url = f"{self.config.host}{self.config.panel_path}/panel/api/inbounds/addClient"
            
            # Важно: settings должен быть строкой JSON
            settings = json.dumps({"clients": [client_data]})
            
            data = {
                "id": inbound_id,
                "settings": settings
            }
            
            logger.debug(f"add_client data: {data}")
            response = self.session.post(url, data=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    logger.info(f"Клиент добавлен в {self.config.name}, inbound {inbound_id}")
                    return True
                else:
                    msg = result.get('msg', '')
                    logger.error(f"Ошибка добавления в {self.config.name}/{inbound_id}: {msg} | client_id={client_data.get('id', 'N/A')[:8]}...")
                    return False
            else:
                logger.error(f"HTTP {response.status_code} в {self.config.name}/{inbound_id}: {response.text[:200]}")
            
            return False
        except Exception as e:
            logger.error(f"Ошибка add_client: {e}")
            return False
    
    def update_client(self, inbound_id: int, client_id: str, client_data: Dict[str, Any]) -> bool:
        """
        Обновить клиента.
        POST {panel_path}/panel/api/inbounds/updateClient/{clientId}
        
        Формат данных тот же, что и для add_client.
        """
        try:
            url = f"{self.config.host}{self.config.panel_path}/panel/api/inbounds/updateClient/{client_id}"
            
            settings = json.dumps({"clients": [client_data]})
            
            data = {
                "id": inbound_id,
                "settings": settings
            }
            
            logger.debug(f"update_client data: {data}")
            response = self.session.post(url, data=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    logger.info(f"Клиент {client_id} обновлен в {self.config.name}")
                    return True
                else:
                    logger.error(f"Ошибка обновления: {result}")
            else:
                logger.error(f"HTTP {response.status_code}: {response.text[:200]}")
            
            return False
        except Exception as e:
            logger.error(f"Ошибка update_client: {e}")
            return False
    
    def delete_client(self, inbound_id: int, client_id: str) -> bool:
        """
        Удалить клиента.
        POST {panel_path}/panel/api/inbounds/{id}/delClient/{clientId}
        """
        try:
            url = f"{self.config.host}{self.config.panel_path}/panel/api/inbounds/{inbound_id}/delClient/{client_id}"
            
            response = self.session.post(url, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    logger.info(f"Клиент {client_id} удален из {self.config.name}")
                    return True
                else:
                    logger.warning(f"delete_client вернул success=false: {result}")
            else:
                logger.error(f"HTTP {response.status_code}: {response.text[:200]}")
            
            return False
        except Exception as e:
            logger.error(f"Ошибка delete_client: {e}")
            return False
    
    def get_client_traffic(self, email: str) -> Dict[str, Any]:
        """
        Получить статистику трафика клиента по email.
        GET {panel_path}/panel/api/inbounds/getClientTraffics/{email}
        """
        try:
            url = f"{self.config.host}{self.config.panel_path}/panel/api/inbounds/getClientTraffics/{email}"
            
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    return result.get("obj", {})
            
            return {}
        except Exception as e:
            logger.error(f"Ошибка get_client_traffic: {e}")
            return {}
    
    def get_client_traffic_by_uuid(self, uuid: str, password: str = None) -> Dict[str, int]:
        """
        Получить суммарный трафик клиента по UUID (или password для Shadowsocks) со всех inbounds.
        Возвращает {'upload': bytes, 'download': bytes}
        """
        total_up = 0
        total_down = 0
        
        try:
            inbounds = self.get_inbounds()
            
            for inbound in inbounds:
                # Determine protocol to know how to match client
                protocol = inbound.get('protocol', 'vless').lower()
                
                # Альтернативный подход: ищем в settings.clients
                settings_str = inbound.get('settings', '{}')
                try:
                    settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                    clients = settings.get('clients', [])
                    
                    for client in clients:
                        # Match by appropriate field based on protocol
                        match = False
                        if protocol == 'shadowsocks' and password:
                            match = client.get('password') == password
                        else:
                            match = client.get('id') == uuid
                        
                        if match:
                            # Нашли клиента, теперь ищем его статистику
                            client_email = client.get('email', '')
                            if client_email:
                                # Получаем статистику по email
                                traffic = self.get_client_traffic(client_email)
                                if traffic:
                                    total_up += traffic.get('up', 0)
                                    total_down += traffic.get('down', 0)
                            break
                except (json.JSONDecodeError, Exception):
                    continue
            
            return {'upload': total_up, 'download': total_down}
        except Exception as e:
            logger.error(f"Ошибка get_client_traffic_by_uuid: {e}")
            return {'upload': 0, 'download': 0}
    
    def get_subscription_content(self, sub_id: str) -> str:
        """
        Получить готовую подписку от 3x-ui панели по sub_id.
        Возвращает base64-encoded контент подписки.
        """
        try:
            # Используем отдельный sub_host если задан, иначе host
            sub_host = (self.config.sub_host or self.config.host).rstrip('/')
            sub_path = self.config.sub_path.rstrip('/')
            url = f"{sub_host}{sub_path}/{sub_id}"
            
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                return response.text
            else:
                logger.warning(f"Panel {self.config.name}: subscription not found at {url}, status={response.status_code}")
                return ""
        except Exception as e:
            logger.error(f"Ошибка get_subscription_content: {e}")
            return ""
    
    def find_client_by_email(self, email: str, inbounds: List[Dict]) -> Optional[Dict]:
        """
        Найти клиента по email в списке inbounds.
        Возвращает {inbound_id, client_id, client_data} или None.
        """
        for inbound in inbounds:
            inbound_id = inbound.get('id')
            settings_str = inbound.get('settings', '{}')
            try:
                settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                for client in settings.get('clients', []):
                    if client.get('email') == email:
                        return {
                            'inbound_id': inbound_id,
                            'client_id': client.get('id'),
                            'client': client
                        }
            except (json.JSONDecodeError, TypeError):
                continue
        return None
    
    def find_client_by_id(self, client_id: str, inbounds: List[Dict]) -> Optional[Dict]:
        """
        Найти клиента по id (UUID) в списке inbounds.
        Возвращает {inbound_id, client_id, client_data} или None.
        """
        for inbound in inbounds:
            inbound_id = inbound.get('id')
            settings_str = inbound.get('settings', '{}')
            try:
                settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                for client in settings.get('clients', []):
                    if client.get('id') == client_id:
                        return {
                            'inbound_id': inbound_id,
                            'client_id': client.get('id'),
                            'client': client
                        }
            except (json.JSONDecodeError, TypeError):
                continue
        return None


class XUIClient:
    """Менеджер для работы с несколькими панелями."""
    
    def __init__(self, panels_config_path: str = "config/panels.yaml"):
        self.panels_config_path = panels_config_path
        self.panels: List[XUIPanel] = []
        self._load_panels()
    
    def _load_panels(self):
        """Загрузить конфигурацию панелей."""
        import yaml
        import os
        
        if not os.path.exists(self.panels_config_path):
            logger.warning(f"Файл конфигурации панелей не найден: {self.panels_config_path}")
            return
        
        try:
            with open(self.panels_config_path, "r") as f:
                config = yaml.safe_load(f)
            
            for panel_data in config.get("panels", []):
                panel_config = PanelConfig(**panel_data)
                panel = XUIPanel(panel_config)
                self.panels.append(panel)
                logger.info(f"Загружена панель: {panel_config.name} ({panel_config.host}), sub_path={panel_config.sub_path}")
            
            # Сортировка по приоритету
            self.panels.sort(key=lambda p: p.config.priority)
            
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации панелей: {e}")
    
    def connect_all(self) -> Dict[str, bool]:
        """Подключиться ко всем панелям."""
        results = {}
        for panel in self.panels:
            results[panel.config.name] = panel.login()
        return results
    
    def get_connected_panels(self) -> List[XUIPanel]:
        """Получить список подключенных панелей."""
        return [p for p in self.panels if p.login()]
    
    def get_panel_by_name(self, name: str) -> Optional[XUIPanel]:
        """Найти панель по имени."""
        for panel in self.panels:
            if panel.config.name == name:
                return panel
        return None
    
    def get_best_panel(self) -> Optional[XUIPanel]:
        """Получить лучшую доступную панель (по приоритету)."""
        connected = self.get_connected_panels()
        return connected[0] if connected else None
