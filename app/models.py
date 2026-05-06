"""Database models."""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class SubscriptionPreset(db.Model):
    """Subscription preset - filters configs by name patterns."""
    
    __tablename__ = 'subscription_presets'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Preset identification
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=True)
    
    # Filter rules (comma-separated patterns)
    include_patterns = db.Column(db.Text, nullable=True)  # Config names must contain these
    exclude_patterns = db.Column(db.Text, nullable=True)  # Config names must NOT contain these
    
    # Metadata
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    subscriptions = db.relationship('Subscription', backref='preset', lazy='dynamic')
    
    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'include_patterns': self.include_patterns,
            'exclude_patterns': self.exclude_patterns,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    def matches_config(self, config_name: str) -> bool:
        """Check if a config name matches this preset's filters."""
        config_name_lower = config_name.lower()
        
        # Check include patterns (all must match at least one)
        if self.include_patterns:
            includes = [p.strip().lower() for p in self.include_patterns.split(',') if p.strip()]
            if includes and not any(p in config_name_lower for p in includes):
                return False
        
        # Check exclude patterns (none must match)
        if self.exclude_patterns:
            excludes = [p.strip().lower() for p in self.exclude_patterns.split(',') if p.strip()]
            if any(p in config_name_lower for p in excludes):
                return False
        
        return True


class Subscription(db.Model):
    """Subscription model - source of truth for users across all panels."""
    
    __tablename__ = 'subscriptions'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # User identification (matches 3x-ui fields)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    uuid = db.Column(db.String(36), unique=True, nullable=True)
    sub_token = db.Column(db.String(64), unique=True, nullable=True, index=True)  # token for subscription link
    
    # Preset for filtering configs
    preset_id = db.Column(db.Integer, db.ForeignKey('subscription_presets.id'), nullable=True)
    
    # Traffic and expiry (GB and days)
    total_gb = db.Column(db.Float, default=0)  # 0 = unlimited, stored in GB for UI
    expiry_days = db.Column(db.Integer, default=0)  # 0 = never expires
    
    # Status
    enabled = db.Column(db.Boolean, default=True)
    
    # 3x-ui specific fields
    flow = db.Column(db.String(50), default='xtls-rprx-vision')  # for VLESS
    sub_id = db.Column(db.String(36), unique=True, nullable=True)  # subId - unique per user, same across all inbounds
    limit_ip = db.Column(db.Integer, default=0)  # limitIp - max concurrent IPs (0 = unlimited)
    tg_id = db.Column(db.String(50), nullable=True)  # tgId - Telegram ID for notifications
    
    # Shadowsocks 2022 specific field
    ss_password = db.Column(db.String(64), nullable=True)  # Password for Shadowsocks 2022
    
    # Sync status
    sync_status = db.Column(db.String(20), default='pending')  # pending, synced, failed
    last_sync_at = db.Column(db.DateTime, nullable=True)
    sync_error = db.Column(db.Text, nullable=True)
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Traffic stats (bytes)
    upload_bytes = db.Column(db.BigInteger, default=0)
    download_bytes = db.Column(db.BigInteger, default=0)
    last_traffic_update = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    sync_logs = db.relationship('SyncLog', backref='subscription', lazy='dynamic',
                                cascade='all, delete-orphan')
    
    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'email': self.email,
            'uuid': self.uuid,
            'sub_token': self.sub_token,
            'sub_id': self.sub_id,
            'preset_id': self.preset_id,
            'total_gb': self.total_gb,
            'expiry_days': self.expiry_days,
            'enabled': self.enabled,
            'flow': self.flow,
            'limit_ip': self.limit_ip,
            'tg_id': self.tg_id,
            'ss_password': self.ss_password,
            'sync_status': self.sync_status,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'sync_error': self.sync_error,
            'upload_bytes': self.upload_bytes or 0,
            'download_bytes': self.download_bytes or 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    def to_xui_client(self, protocol='vless'):
        """Convert to 3x-ui client format for specific protocol.
        
        Args:
            protocol: Protocol type (vless, vmess, shadowsocks, trojan)
        
        Returns:
            Dict with client data appropriate for the protocol
        """
        import uuid as uuid_lib
        
        # Если uuid пустой или None, генерируем новый
        client_id = (self.uuid and self.uuid.strip()) or str(uuid_lib.uuid4())
        
        # subId должен быть одинаковым во всех inbound'ах
        sub_id = (self.sub_id and self.sub_id.strip()) or client_id
        
        # Calculate expiry timestamp (milliseconds)
        expiry_time = 0
        if self.expiry_days > 0:
            from datetime import timedelta
            expiry_date = datetime.utcnow() + timedelta(days=self.expiry_days)
            expiry_time = int(expiry_date.timestamp() * 1000)
        
        # totalGB передаётся в байтах: GB * 1024^3
        total_bytes = int(self.total_gb * 1024 * 1024 * 1024) if self.total_gb else 0
        
        # Base fields common to all protocols
        result = {
            'email': self.email,
            'subId': sub_id,
            'limitIp': self.limit_ip if self.limit_ip else 0,
            'totalGB': total_bytes,
            'expiryTime': expiry_time,
            'enable': self.enabled,
        }
        
        # Protocol-specific fields
        protocol = protocol.lower()
        
        if protocol in ('vless', 'vmess', 'trojan'):
            # These protocols use UUID as client ID
            result['id'] = client_id
            if self.tg_id:
                result['tgId'] = self.tg_id
            # flow is determined per-inbound in sync_service based on transport type
            
        elif protocol == 'shadowsocks':
            # Shadowsocks 2022 uses password instead of UUID
            # Generate password if not set (32 byte hex for 2022-blake3-aes-256-gcm)
            if not self.ss_password:
                self.ss_password = uuid_lib.uuid4().hex + uuid_lib.uuid4().hex  # 64 hex chars = 32 bytes
                # Note: caller should save this to DB
            result['password'] = self.ss_password
            # Shadowsocks doesn't use id, subId, or tgId in the same way
            # But we keep subId for subscription link generation
            
        else:
            # Default fallback
            result['id'] = client_id
            if self.tg_id:
                result['tgId'] = self.tg_id
            
        return result
    
    @classmethod
    def from_xui_client(cls, client_data):
        """Create Subscription from 3x-ui client data."""
        from datetime import datetime
        
        expiry_days = 0
        if client_data.get('expiryTime'):
            expiry_ts = client_data['expiryTime'] / 1000
            expiry_date = datetime.fromtimestamp(expiry_ts)
            days_diff = (expiry_date - datetime.utcnow()).days
            expiry_days = max(0, days_diff)
        
        # Конвертируем байты обратно в GB
        total_gb = 0
        total_bytes = client_data.get('totalGB', 0)
        if total_bytes:
            total_gb = round(total_bytes / (1024 * 1024 * 1024), 2)
        
        return cls(
            email=client_data.get('email', ''),
            uuid=client_data.get('id'),
            sub_id=client_data.get('subId'),
            total_gb=total_gb,
            expiry_days=expiry_days,
            enabled=client_data.get('enable', True),
            flow=client_data.get('flow', ''),
            limit_ip=client_data.get('limitIp', 0),
            tg_id=client_data.get('tgId'),
            ss_password=client_data.get('password'),  # Shadowsocks password
            sync_status='synced'
        )


class SyncLog(db.Model):
    """Log of synchronization attempts with panels."""
    
    __tablename__ = 'sync_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=False)
    panel_name = db.Column(db.String(100), nullable=False)
    
    action = db.Column(db.String(20), nullable=False)  # create, update, delete
    status = db.Column(db.String(20), nullable=False)  # success, failed
    
    error_message = db.Column(db.Text, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'subscription_id': self.subscription_id,
            'panel_name': self.panel_name,
            'action': self.action,
            'status': self.status,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class GlobalSettings(db.Model):
    """Global subscription settings applied to all users."""
    
    __tablename__ = 'global_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Subscription metadata
    sub_title = db.Column(db.String(100), default='VPN Subscription')
    sub_description = db.Column(db.Text, nullable=True)
    
    # Default values for new subscriptions
    default_total_gb = db.Column(db.Float, default=0)  # 0 = unlimited
    default_expiry_days = db.Column(db.Integer, default=30)
    
    # Clash-specific global settings
    custom_rules = db.Column(db.Text, nullable=True)  # YAML lines added to Clash config
    custom_direct_countries = db.Column(db.String(255), nullable=True)  # comma-separated country codes
    
    # Auto-sync settings
    auto_sync_enabled = db.Column(db.Boolean, default=True)
    auto_sync_interval_minutes = db.Column(db.Integer, default=30)  # минуты между синхронизациями
    
    # Happ subscription metadata
    sub_expire_enabled = db.Column(db.Boolean, default=False)
    sub_expire_button_link = db.Column(db.String(255), nullable=True)
    sub_info_button_text = db.Column(db.String(25), nullable=True)
    sub_info_button_link = db.Column(db.String(255), nullable=True)
    announce_text = db.Column(db.Text, nullable=True)
    fallback_url = db.Column(db.String(255), nullable=True)
    profile_web_page_url = db.Column(db.String(255), nullable=True)
    support_url = db.Column(db.String(255), nullable=True)
    
    # Happ custom routing (JSON for custom-tunnel-config)
    happ_routing_enabled = db.Column(db.Boolean, default=False)
    happ_routing_config = db.Column(db.Text, nullable=True)
    
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    @classmethod
    def get(cls):
        """Get singleton settings record."""
        settings = cls.query.first()
        if not settings:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings
    
    def to_dict(self):
        return {
            'id': self.id,
            'sub_title': self.sub_title,
            'sub_description': self.sub_description,
            'default_total_gb': self.default_total_gb,
            'default_expiry_days': self.default_expiry_days,
            'custom_rules': self.custom_rules,
            'custom_direct_countries': self.custom_direct_countries,
            'auto_sync_enabled': self.auto_sync_enabled,
            'auto_sync_interval_minutes': self.auto_sync_interval_minutes,
            'sub_expire_enabled': self.sub_expire_enabled,
            'sub_expire_button_link': self.sub_expire_button_link,
            'sub_info_button_text': self.sub_info_button_text,
            'sub_info_button_link': self.sub_info_button_link,
            'announce_text': self.announce_text,
            'fallback_url': self.fallback_url,
            'profile_web_page_url': self.profile_web_page_url,
            'support_url': self.support_url,
            'happ_routing_enabled': self.happ_routing_enabled,
            'happ_routing_config': self.happ_routing_config,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class PanelState(db.Model):
    """Track known state of panels for diffing."""
    
    __tablename__ = 'panel_states'
    
    id = db.Column(db.Integer, primary_key=True)
    
    panel_name = db.Column(db.String(100), nullable=False, unique=True)
    inbound_id = db.Column(db.Integer, nullable=False)
    
    # JSON snapshot of clients on this panel
    clients_snapshot = db.Column(db.Text, nullable=True)
    
    last_check_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'panel_name': self.panel_name,
            'inbound_id': self.inbound_id,
            'last_check_at': self.last_check_at.isoformat() if self.last_check_at else None
        }
