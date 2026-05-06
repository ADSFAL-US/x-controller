"""Flask application for 3x-controller."""

import base64
import functools
import json
import logging
import os
import secrets
import urllib.parse
import uuid as uuid_lib
from datetime import datetime
from typing import List
from flask import Flask, jsonify, request, render_template_string, redirect, url_for, session

from app.xui_client import XUIClient
from app.models import db, Subscription, SyncLog, GlobalSettings, SubscriptionPreset
from app.sync_service import SyncService

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Создание Flask приложения
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Конфигурация БД (абсолютный путь для Docker)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:////app/data/subscriptions.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Инициализация БД
db.init_app(app)

# Создаем таблицы при старте (с защитой от race condition в multi-worker gunicorn)
with app.app_context():
    try:
        from sqlalchemy import inspect, text
        from sqlalchemy.exc import OperationalError
        inspector = inspect(db.engine)
        existing_tables = inspector.get_table_names()
        if 'subscriptions' not in existing_tables:
            db.create_all()
            logger.info("Database tables created")
        else:
            logger.info("Database tables already exist")
        
        # Run migrations for missing columns
        if 'global_settings' in existing_tables:
            existing_columns = {col['name'] for col in inspector.get_columns('global_settings')}
            
            migrations = [
                ('sub_expire_enabled', 'BOOLEAN DEFAULT 0'),
                ('sub_expire_button_link', 'VARCHAR(255)'),
                ('sub_info_button_text', 'VARCHAR(25)'),
                ('sub_info_button_link', 'VARCHAR(255)'),
                ('announce_text', 'TEXT'),
                ('fallback_url', 'VARCHAR(255)'),
                ('profile_web_page_url', 'VARCHAR(255)'),
                ('support_url', 'VARCHAR(255)'),
                ('happ_routing_enabled', 'BOOLEAN DEFAULT 0'),
                ('happ_routing_config', 'TEXT'),
            ]
            
            for col_name, col_type in migrations:
                if col_name not in existing_columns:
                    try:
                        db.session.execute(text(f"ALTER TABLE global_settings ADD COLUMN {col_name} {col_type}"))
                        db.session.commit()
                        logger.info(f"Migration: added column {col_name} to global_settings")
                    except OperationalError as e:
                        logger.warning(f"Migration: column {col_name} may already exist: {e}")
                        db.session.rollback()
        
        # Migration: Add preset_id to subscriptions table if it exists
        if 'subscriptions' in existing_tables:
            existing_columns = {col['name'] for col in inspector.get_columns('subscriptions')}
            if 'preset_id' not in existing_columns:
                try:
                    db.session.execute(text("ALTER TABLE subscriptions ADD COLUMN preset_id INTEGER"))
                    db.session.commit()
                    logger.info("Migration: added preset_id column to subscriptions")
                except OperationalError as e:
                    logger.warning(f"Migration: preset_id column may already exist: {e}")
                    db.session.rollback()
            
            # Migration: Add ss_password for Shadowsocks 2022 support
            if 'ss_password' not in existing_columns:
                try:
                    db.session.execute(text("ALTER TABLE subscriptions ADD COLUMN ss_password VARCHAR(64)"))
                    db.session.commit()
                    logger.info("Migration: added ss_password column to subscriptions")
                except OperationalError as e:
                    logger.warning(f"Migration: ss_password column may already exist: {e}")
                    db.session.rollback()
            
            # Migration: Add expire_at for absolute expiration timestamp
            if 'expire_at' not in existing_columns:
                try:
                    db.session.execute(text("ALTER TABLE subscriptions ADD COLUMN expire_at TIMESTAMP"))
                    db.session.commit()
                    logger.info("Migration: added expire_at column to subscriptions")
                except OperationalError as e:
                    logger.warning(f"Migration: expire_at column may already exist: {e}")
                    db.session.rollback()
    except Exception:
        # Race condition: другой worker уже создал таблицы
        logger.info("Database tables likely created by another worker")

# Инициализация клиента 3x-ui
xui_client = XUIClient("config/panels.yaml")

# Инициализация сервиса синхронизации
sync_service = SyncService(xui_client)


# ==================== Authentication ====================

def check_auth(username, password):
    """Check if username/password combination is valid."""
    expected_user = os.environ.get('ADMIN_USERNAME', 'admin')
    expected_pass = os.environ.get('ADMIN_PASSWORD', 'admin')
    return username == expected_user and password == expected_pass


def require_auth(f):
    """Decorator to require authentication for a route."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.is_json or request.path.startswith('/api/'):
                # API request - check for Basic Auth
                auth = request.headers.get('Authorization', '')
                if auth.startswith('Basic '):
                    import base64
                    try:
                        decoded = base64.b64decode(auth[6:]).decode('utf-8')
                        username, password = decoded.split(':', 1)
                        if check_auth(username, password):
                            return f(*args, **kwargs)
                    except Exception:
                        pass
                return jsonify({'error': 'Authentication required'}), 401
            else:
                # Web request - redirect to login
                return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page for web UI."""
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if check_auth(username, password):
            session['authenticated'] = True
            next_url = request.args.get('next') or url_for('index')
            return redirect(next_url)
        else:
            error = 'Invalid username or password'
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head><title>Login - 3x-controller</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f5f5f5; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-box { background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 300px; }
        h1 { margin: 0 0 20px 0; color: #333; text-align: center; }
        .form-group { margin: 15px 0; }
        label { display: block; margin-bottom: 5px; color: #666; }
        input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
        button:hover { background: #0056b3; }
        .error { color: #dc3545; margin-top: 10px; text-align: center; }
    </style>
    </head>
    <body>
        <div class="login-box">
            <h1>3x-controller</h1>
            <form method="POST">
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" name="username" required autofocus>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" name="password" required>
                </div>
                <button type="submit">Login</button>
                {% if error %}
                <div class="error">{{ error }}</div>
                {% endif %}
            </form>
        </div>
    </body>
    </html>
    """, error=error)


@app.route('/logout')
def logout():
    """Logout and clear session."""
    session.pop('authenticated', None)
    return redirect(url_for('login'))


# ==================== Web UI Routes (Protected) ====================

@app.route('/')
@require_auth
def index():
    """Главная страница - дашборд."""
    with app.app_context():
        db.create_all()
    
    total = Subscription.query.count()
    active = Subscription.query.filter_by(enabled=True).count()
    pending_sync = Subscription.query.filter_by(sync_status='pending').count()
    failed_sync = Subscription.query.filter_by(sync_status='failed').count()
    
    panels_status = xui_client.connect_all()
    
    panels_html = ""
    for name, connected in panels_status.items():
        status_class = "connected" if connected else "disconnected"
        status_text = "Connected" if connected else "Disconnected"
        panels_html += f'<div class="panel {status_class}"><strong>{name}</strong>: {status_text}</div>'
    
    return render_template_string(f"""
    <!DOCTYPE html>
    <html>
    <head><title>3x-controller</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .stats {{ display: flex; gap: 20px; margin: 20px 0; }}
        .stat {{ padding: 20px; border: 1px solid #ddd; border-radius: 8px; min-width: 150px; }}
        .stat h3 {{ margin: 0 0 10px 0; color: #666; }}
        .stat .value {{ font-size: 32px; font-weight: bold; color: #333; }}
        .nav {{ margin: 20px 0; }}
        .nav a {{ margin-right: 20px; text-decoration: none; color: #007bff; }}
        .nav a:hover {{ text-decoration: underline; }}
        .panels {{ margin: 20px 0; padding: 20px; background: #f5f5f5; border-radius: 8px; }}
        .panel {{ margin: 10px 0; padding: 10px; background: white; border-radius: 4px; }}
        .connected {{ border-left: 4px solid #28a745; }}
        .disconnected {{ border-left: 4px solid #dc3545; }}
    </style>
    </head>
    <body>
        <h1>3x-controller Dashboard</h1>
        <div class="nav">
            <a href="/subscriptions">Subscriptions</a>
            <a href="/subscriptions/new">Create Subscription</a>
            <a href="/presets">Presets</a>
            <a href="/settings">Settings</a>
            <a href="/api/health">API Health</a>
            <a href="/logout" style="float:right;">Logout</a>
        </div>
        <h2>Statistics</h2>
        <div class="stats">
            <div class="stat"><h3>Total</h3><div class="value">{total}</div></div>
            <div class="stat"><h3>Active</h3><div class="value">{active}</div></div>
            <div class="stat"><h3>Pending</h3><div class="value">{pending_sync}</div></div>
            <div class="stat"><h3>Failed</h3><div class="value">{failed_sync}</div></div>
        </div>
        <h2>Panels Status</h2>
        <div class="panels">{panels_html}</div>
    </body>
    </html>
    """)


@app.route('/subscriptions')
@require_auth
def list_subscriptions():
    """Список всех подписок."""
    subs = Subscription.query.order_by(Subscription.created_at.desc()).all()
    
    rows = ""
    for sub in subs:
        status_badge = {
            'synced': '<span style="color: green;">Synced</span>',
            'pending': '<span style="color: orange;">Pending</span>',
            'failed': '<span style="color: red;">Failed</span>'
        }.get(sub.sync_status, sub.sync_status)
        
        sub_link = f'<a href="/sub/{sub.sub_token}" target="_blank" style="font-size:11px;">Sub</a>' if sub.sub_token else 'N/A'
        
        # Get preset name if assigned
        preset_name = '-'
        if sub.preset_id:
            preset = SubscriptionPreset.query.get(sub.preset_id)
            if preset:
                preset_name = preset.name
        
        rows += f"""
        <tr>
            <td>{sub.id}</td>
            <td>{sub.email}</td>
            <td>{sub.uuid or 'Auto'}</td>
            <td>{preset_name}</td>
            <td>{sub.total_gb} GB</td>
            <td>{sub.expiry_days} days</td>
            <td>{'Enabled' if sub.enabled else 'Disabled'}</td>
            <td>{status_badge}</td>
            <td>{sub_link}</td>
            <td>
                <a href="/subscriptions/{sub.id}/edit">Edit</a>
                <button onclick="syncSub({sub.id})" style="margin-left:5px;cursor:pointer;">Sync</button>
                <form method="POST" action="/subscriptions/{sub.id}/delete" style="display:inline;">
                    <button type="submit" onclick="return confirm('Delete?')">Delete</button>
                </form>
            </td>
        </tr>
        """
    
    return render_template_string(f"""
    <!DOCTYPE html>
    <html>
    <head><title>Subscriptions - 3x-controller</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 14px; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background-color: #f5f5f5; }}
        .nav {{ margin: 20px 0; }}
        .nav a {{ margin-right: 20px; text-decoration: none; color: #007bff; }}
        .btn {{ padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 4px; }}
        .btn-orange {{ background: #ff9800; }}
        .btn-green {{ background: #4caf50; }}
        .sub-link {{ font-family: monospace; font-size: 11px; word-break: break-all; max-width: 150px; }}
    </style>
    <script>
    async function syncSub(id) {{
        if (!confirm('Force sync subscription ' + id + '?')) return;
        try {{
            const resp = await fetch('/api/sync/' + id, {{ method: 'POST' }});
            const data = await resp.json();
            if (data.success) {{
                alert('Sync scheduled!');
                location.reload();
            }} else {{
                alert('Error: ' + data.error);
            }}
        }} catch (e) {{
            alert('Error: ' + e);
        }}
    }}
    async function syncAll() {{
        if (!confirm('Force sync ALL subscriptions?')) return;
        try {{
            const resp = await fetch('/api/sync/all', {{ method: 'POST' }});
            const data = await resp.json();
            if (data.success) {{
                alert('Scheduled ' + data.results.length + ' syncs');
                location.reload();
            }} else {{
                alert('Error: ' + data.error);
            }}
        }} catch (e) {{
            alert('Error: ' + e);
        }}
    }}
    </script>
    </head>
    <body>
        <h1>Subscriptions</h1>
        <div class="nav">
            <a href="/">Dashboard</a>
            <a href="/presets">Presets</a>
            <a href="/subscriptions/new" class="btn">Create New</a>
            <button onclick="syncAll()" class="btn btn-orange" style="padding: 10px 20px; border: none; cursor: pointer; border-radius: 4px;">Sync All</button>
            <a href="/logout" style="float:right;">Logout</a>
        </div>
        <table>
            <tr>
                <th>ID</th><th>Email</th><th>UUID</th><th>Preset</th><th>Traffic</th>
                <th>Expiry</th><th>Status</th><th>Sync</th><th>Sub</th><th>Actions</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>
    """)


@app.route('/subscriptions/new', methods=['GET', 'POST'])
@require_auth
def create_subscription_form():
    """Форма создания подписки с ВСЕМИ полями 3x-ui."""
    if request.method == 'POST':
        # Получаем все поля из формы
        email = request.form.get('email')
        uuid_val = request.form.get('uuid', '').strip()
        uuid = uuid_val if uuid_val else str(uuid_lib.uuid4())
        settings = GlobalSettings.get()
        total_gb = float(request.form.get('total_gb', settings.default_total_gb or 0))
        expiry_days = int(request.form.get('expiry_days', settings.default_expiry_days or 0))
        enabled = request.form.get('enabled') == 'on'
        flow = request.form.get('flow', 'xtls-rprx-vision')
        
        # Preset selection
        preset_id = request.form.get('preset_id')
        if preset_id:
            try:
                preset_id = int(preset_id)
                # Verify preset exists
                if not SubscriptionPreset.query.get(preset_id):
                    preset_id = None
            except (ValueError, TypeError):
                preset_id = None
        
        # Валидация
        if not email:
            return "Email is required", 400
        
        # Проверка уникальности email
        if Subscription.query.filter_by(email=email).first():
            return f"Subscription with email {email} already exists", 409
        
        # Создаем подписку
        from datetime import timedelta
        expire_at = None
        if expiry_days > 0:
            expire_at = datetime.utcnow() + timedelta(days=expiry_days)
        
        sub = Subscription(
            email=email,
            uuid=uuid,
            sub_token=secrets.token_urlsafe(16),
            preset_id=preset_id,
            total_gb=total_gb,
            expiry_days=expiry_days,
            expire_at=expire_at,
            enabled=enabled,
            flow=flow,
            sync_status='pending'
        )
        
        db.session.add(sub)
        db.session.commit()
        
        # Запускаем синхронизацию
        sync_service.schedule_sync(sub, 'create')
        
        return redirect(url_for('list_subscriptions'))
    
    # GET - показываем форму со всеми полями 3x-ui
    settings = GlobalSettings.get()
    presets = SubscriptionPreset.query.filter_by(is_active=True).all()
    
    # Build preset options HTML
    preset_options = '<option value="">-- No preset (all configs) --</option>'
    for p in presets:
        desc = f" - {p.description}" if p.description else ""
        preset_options += f'<option value="{p.id}">{p.name}{desc}</option>'
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head><title>Create Subscription - 3x-controller</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; max-width: 600px; }
        .form-group { margin: 15px 0; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input, select { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
        .checkbox { width: auto; }
        button { padding: 10px 20px; background: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; }
        button:hover { background: #218838; }
        .nav { margin-bottom: 20px; }
        .nav a { text-decoration: none; color: #007bff; }
        .defaults-info { font-size: 12px; color: #666; margin-top: 4px; }
        .help { font-size: 12px; color: #666; margin-top: 4px; }
    </style>
    </head>
    <body>
        <div class="nav"><a href="/subscriptions">&larr; Back to list</a></div>
        <h1>Create Subscription</h1>
        <form method="POST">
            <div class="form-group">
                <label>Email *</label>
                <input type="email" name="email" required placeholder="user@example.com">
            </div>
            <div class="form-group">
                <label>UUID (optional, auto-generated if empty)</label>
                <input type="text" name="uuid" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx">
            </div>
            <div class="form-group">
                <label>Preset</label>
                <select name="preset_id">
                    {{ preset_options|safe }}
                </select>
                <div class="help">Filter which configs are included based on config name patterns. <a href="/presets">Manage presets</a></div>
            </div>
            <div class="form-group">
                <label>Traffic Limit (GB, 0 = unlimited)</label>
                <input type="number" name="total_gb" value="{{ default_total_gb }}" min="0" step="0.1">
                <div class="defaults-info">Default from settings: {{ default_total_gb }} GB</div>
            </div>
            <div class="form-group">
                <label>Expiry Days (0 = never)</label>
                <input type="number" name="expiry_days" value="{{ default_expiry_days }}" min="0">
                <div class="defaults-info">Default from settings: {{ default_expiry_days }} days</div>
            </div>
            <div class="form-group">
                <label>
                    <input type="checkbox" name="enabled" class="checkbox" checked> Enabled
                </label>
            </div>
            <div class="form-group">
                <label>Flow (VLESS only)</label>
                <select name="flow">
                    <option value="xtls-rprx-vision" selected>xtls-rprx-vision</option>
                    <option value="xtls-rprx-vision-udp443">xtls-rprx-vision-udp443</option>
                    <option value="">None</option>
                </select>
            </div>
            <button type="submit">Create Subscription</button>
        </form>
    </body>
    </html>
    """, default_total_gb=settings.default_total_gb or 0, default_expiry_days=settings.default_expiry_days or 30, preset_options=preset_options)


@app.route('/subscriptions/<subscription_id>/edit', methods=['GET', 'POST'])
@require_auth
def edit_subscription(subscription_id):
    """Редактирование подписки с ВСЕМИ полями 3x-ui."""
    sub = Subscription.query.get(subscription_id)
    if not sub:
        return "Subscription not found", 404
    
    if request.method == 'POST':
        # Обновляем все поля
        new_email = request.form.get('email')
        
        # Проверка уникальности email если изменился
        if new_email != sub.email:
            if Subscription.query.filter_by(email=new_email).first():
                return f"Email {new_email} already in use", 409
            sub.email = new_email
        
        sub.uuid = request.form.get('uuid') or sub.uuid
        sub.total_gb = float(request.form.get('total_gb', sub.total_gb))
        
        # Update expiry and recalculate absolute timestamp
        new_expiry_days = int(request.form.get('expiry_days', sub.expiry_days))
        if new_expiry_days != sub.expiry_days:
            sub.expiry_days = new_expiry_days
            if new_expiry_days > 0:
                from datetime import timedelta
                sub.expire_at = datetime.utcnow() + timedelta(days=new_expiry_days)
            else:
                sub.expire_at = None
        
        sub.enabled = request.form.get('enabled') == 'on'
        sub.flow = request.form.get('flow', sub.flow)
        
        # Preset selection
        preset_id = request.form.get('preset_id')
        if preset_id:
            try:
                preset_id = int(preset_id)
                if not SubscriptionPreset.query.get(preset_id):
                    preset_id = None
            except (ValueError, TypeError):
                preset_id = None
        else:
            preset_id = None
        sub.preset_id = preset_id
        
        sub.sync_status = 'pending'
        sub.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        # Запускаем синхронизацию
        sync_service.schedule_sync(sub, 'update')
        
        return redirect(url_for('list_subscriptions'))
    
    # GET - форма редактирования со всеми полями
    checked = 'checked' if sub.enabled else ''
    presets = SubscriptionPreset.query.filter_by(is_active=True).all()
    
    # Build preset options HTML
    preset_options = '<option value="">-- No preset (all configs) --</option>'
    for p in presets:
        desc = f" - {p.description}" if p.description else ""
        selected = 'selected' if sub.preset_id == p.id else ''
        preset_options += f'<option value="{p.id}" {selected}>{p.name}{desc}</option>'
    
    return render_template_string(f"""
    <!DOCTYPE html>
    <html>
    <head><title>Edit Subscription - 3x-controller</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; max-width: 600px; }}
        .form-group {{ margin: 15px 0; }}
        label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
        input, select {{ width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
        .checkbox {{ width: auto; }}
        button {{ padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }}
        button:hover {{ background: #0056b3; }}
        .nav {{ margin-bottom: 20px; }}
        .nav a {{ text-decoration: none; color: #007bff; }}
        .uuid {{ font-family: monospace; font-size: 12px; color: #666; }}
        .help {{ font-size: 12px; color: #666; margin-top: 4px; }}
    </style>
    </head>
    <body>
        <div class="nav"><a href="/subscriptions">&larr; Back to list</a></div>
        <h1>Edit Subscription #{sub.id}</h1>
        <form method="POST">
            <div class="form-group">
                <label>Email *</label>
                <input type="email" name="email" value="{sub.email}" required>
            </div>
            <div class="form-group">
                <label>UUID</label>
                <input type="text" name="uuid" value="{sub.uuid or ''}" class="uuid">
                <small>Current: {sub.uuid or 'Auto-generated'}</small>
            </div>
            <div class="form-group">
                <label>Preset</label>
                <select name="preset_id">
                    {{ preset_options|safe }}
                </select>
                <div class="help">Filter which configs are included based on config name patterns. <a href="/presets">Manage presets</a></div>
            </div>
            <div class="form-group">
                <label>Traffic Limit (GB, 0 = unlimited)</label>
                <input type="number" name="total_gb" value="{sub.total_gb}" min="0" step="0.1">
            </div>
            <div class="form-group">
                <label>Expiry Days (0 = never)</label>
                <input type="number" name="expiry_days" value="{sub.expiry_days}" min="0">
            </div>
            <div class="form-group">
                <label>
                    <input type="checkbox" name="enabled" class="checkbox" {checked}> Enabled
                </label>
            </div>
            <div class="form-group">
                <label>Flow (VLESS only)</label>
                <select name="flow">
                    <option value="xtls-rprx-vision" {'selected' if sub.flow == 'xtls-rprx-vision' else ''}>xtls-rprx-vision</option>
                    <option value="xtls-rprx-vision-udp443" {'selected' if sub.flow == 'xtls-rprx-vision-udp443' else ''}>xtls-rprx-vision-udp443</option>
                    <option value="" {'selected' if not sub.flow else ''}>None</option>
                </select>
            </div>
            <button type="submit">Update Subscription</button>
        </form>
    </body>
    </html>
    """, preset_options=preset_options)


@app.route('/subscriptions/<subscription_id>/delete', methods=['POST'])
@require_auth
def delete_subscription_form(subscription_id):
    """Удаление подписки через форму."""
    sub = Subscription.query.get(subscription_id)
    if not sub:
        return "Subscription not found", 404
    
    # Запускаем синхронизацию удаления перед удалением из БД
    sync_service.schedule_sync(sub, 'delete')
    
    # Удаляем из БД
    db.session.delete(sub)
    db.session.commit()
    
    return redirect(url_for('list_subscriptions'))


# ==================== REST API ====================

@app.route('/api/health', methods=['GET'])
@require_auth
def health():
    """Health check endpoint."""
    connected = xui_client.connect_all()
    any_connected = any(connected.values())
    
    return jsonify({
        'status': 'healthy' if any_connected else 'degraded',
        'panels': connected,
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/subscriptions', methods=['POST'])
@require_auth
def create_subscription_api():
    """Создать подписку (REST API)."""
    data = request.get_json() or {}
    
    email = data.get('email')
    if not email:
        return jsonify({'error': 'Email is required'}), 400
    
    # Проверка уникальности
    if Subscription.query.filter_by(email=email).first():
        return jsonify({'error': f'Email {email} already exists'}), 409
    
    # Создаем подписку со всеми полями из 3x-ui
    uuid_val = str(data.get('uuid', '')).strip()
    
    # Handle preset_id
    preset_id = data.get('preset_id')
    if preset_id:
        try:
            preset_id = int(preset_id)
            # Verify preset exists
            if not SubscriptionPreset.query.get(preset_id):
                preset_id = None
        except (ValueError, TypeError):
            preset_id = None
    
    # Calculate absolute expiration timestamp
    expiry_days = int(data.get('expiry_days', 0))
    expire_at = None
    if expiry_days > 0:
        from datetime import timedelta
        expire_at = datetime.utcnow() + timedelta(days=expiry_days)
    
    sub = Subscription(
        email=email,
        uuid=uuid_val if uuid_val else str(uuid_lib.uuid4()),
        sub_token=secrets.token_urlsafe(16),
        preset_id=preset_id,
        total_gb=float(data.get('total_gb', 0)),
        expiry_days=expiry_days,
        expire_at=expire_at,
        enabled=data.get('enabled', True),
        flow=data.get('flow', 'xtls-rprx-vision'),
        sync_status='pending'
    )
    
    db.session.add(sub)
    db.session.commit()
    
    # Запускаем синхронизацию
    sync_service.schedule_sync(sub, 'create')
    
    logger.info(f"Created subscription via API: {email}")
    
    return jsonify({
        'success': True,
        'subscription': sub.to_dict()
    }), 201


@app.route('/api/subscriptions/<subscription_id>', methods=['GET'])
@require_auth
def get_subscription(subscription_id):
    """Получить подписку по ID."""
    sub = Subscription.query.get(subscription_id)
    if not sub:
        return jsonify({'error': 'Subscription not found'}), 404
    
    return jsonify({
        'success': True,
        'subscription': sub.to_dict()
    })


@app.route('/api/subscriptions/<subscription_id>', methods=['PUT'])
@require_auth
def update_subscription_api(subscription_id):
    """Обновить подписку (REST API)."""
    sub = Subscription.query.get(subscription_id)
    if not sub:
        return jsonify({'error': 'Subscription not found'}), 404
    
    data = request.get_json() or {}
    
    # Обновляем все поля 3x-ui
    if 'email' in data:
        new_email = data['email']
        if new_email != sub.email and Subscription.query.filter_by(email=new_email).first():
            return jsonify({'error': f'Email {new_email} already in use'}), 409
        sub.email = new_email
    
    if 'uuid' in data:
        sub.uuid = data['uuid']
    if 'preset_id' in data:
        preset_id = data['preset_id']
        if preset_id:
            try:
                preset_id = int(preset_id)
                if SubscriptionPreset.query.get(preset_id):
                    sub.preset_id = preset_id
                else:
                    return jsonify({'error': f'Preset with id {preset_id} not found'}), 404
            except (ValueError, TypeError):
                return jsonify({'error': 'preset_id must be an integer'}), 400
        else:
            sub.preset_id = None
    if 'total_gb' in data:
        sub.total_gb = float(data['total_gb'])
    if 'expiry_days' in data:
        sub.expiry_days = int(data['expiry_days'])
    if 'enabled' in data:
        sub.enabled = bool(data['enabled'])
    if 'flow' in data:
        sub.flow = data['flow']
    
    sub.sync_status = 'pending'
    sub.updated_at = datetime.utcnow()
    
    db.session.commit()
    
    # Запускаем синхронизацию
    sync_service.schedule_sync(sub, 'update')
    
    logger.info(f"Updated subscription via API: {sub.email}")
    
    return jsonify({
        'success': True,
        'subscription': sub.to_dict()
    })


@app.route('/api/subscriptions/<subscription_id>', methods=['DELETE'])
@require_auth
def delete_subscription_api(subscription_id):
    """Удалить подписку (REST API)."""
    sub = Subscription.query.get(subscription_id)
    if not sub:
        return jsonify({'error': 'Subscription not found'}), 404
    
    # Синхронизируем удаление
    sync_service.schedule_sync(sub, 'delete')
    
    email = sub.email
    db.session.delete(sub)
    db.session.commit()
    
    logger.info(f"Deleted subscription via API: {email}")
    
    return jsonify({
        'success': True,
        'message': f'Subscription {subscription_id} ({email}) deleted'
    })


@app.route('/api/subscriptions', methods=['GET'])
@require_auth
def list_subscriptions_api():
    """Список всех подписок (REST API)."""
    subs = Subscription.query.order_by(Subscription.created_at.desc()).all()
    
    return jsonify({
        'success': True,
        'count': len(subs),
        'subscriptions': [s.to_dict() for s in subs]
    })


@app.route('/api/panels', methods=['GET'])
@require_auth
def list_panels():
    """Список всех панелей и их статус."""
    panels_info = []
    for panel in xui_client.panels:
        panels_info.append({
            'name': panel.config.name,
            'host': panel.config.host,
            'priority': panel.config.priority,
            'max_clients': panel.config.max_clients,
            'connected': panel.login()
        })
    
    return jsonify({'panels': panels_info})


@app.route('/sub/<token>')
def subscription_link(token):
    """Subscription endpoint - serves configs for VPN clients or HTML guide for browsers."""
    sub = Subscription.query.filter_by(sub_token=token).first_or_404()
    
    user_agent = request.headers.get('User-Agent', '').lower()
    is_browser = any(kw in user_agent for kw in ['mozilla', 'chrome', 'safari', 'firefox', 'edge', 'opera'])
    is_clash = 'clash' in user_agent or request.args.get('format') == 'clash'
    
    # Load global settings
    gsettings = GlobalSettings.get()
    
    # Browser → HTML guide
    if is_browser and not is_clash:
        return render_template_string(SUBSCRIPTION_GUIDE_HTML, 
                                     token=token, 
                                     email=sub.email,
                                     sub_url=f"{request.host_url}sub/{token}")
    
    # Collect subscription content from all panels
    all_uris = []
    for panel in xui_client.panels:
        try:
            panel.login()
            client_sub_id = None
            
            # ALWAYS find client on THIS panel by UUID (or password for Shadowsocks)
            # Each panel generates its own subId - cannot use global sub_id
            inbounds = panel.get_inbounds()
            for inbound in inbounds:
                settings_str = inbound.get('settings', '{}')
                protocol = inbound.get('protocol', 'vless').lower()
                try:
                    settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                    clients = settings.get('clients', [])
                    for client in clients:
                        # Match by appropriate field based on protocol
                        match = False
                        if protocol == 'shadowsocks':
                            match = client.get('password') == sub.ss_password
                        else:
                            match = client.get('id') == sub.uuid
                        
                        if match:
                            client_sub_id = client.get('subId') or client.get('id')
                            logger.debug(f"Panel {panel.config.name}: found client with subId={client_sub_id}")
                            break
                    if client_sub_id:
                        break
                except Exception:
                    continue
            
            if client_sub_id:
                # Get ready subscription from panel using sub_id
                sub_content = panel.get_subscription_content(client_sub_id)
                if sub_content:
                    try:
                        decoded = base64.b64decode(sub_content).decode('utf-8')
                        uris = [u.strip() for u in decoded.split('\n') if u.strip()]
                        all_uris.extend(uris)
                        logger.info(f"Panel {panel.config.name}: added {len(uris)} configs via sub_id={client_sub_id}")
                    except Exception as e:
                        logger.warning(f"Panel {panel.config.name}: failed to decode subscription: {e}")
                else:
                    logger.warning(f"Panel {panel.config.name}: empty subscription for sub_id={client_sub_id}")
            else:
                logger.warning(f"Panel {panel.config.name}: client not found for uuid={sub.uuid}")
        except Exception:
            logger.exception(f"Failed to collect configs from {panel.config.name}")
    
    # Filter URIs by preset if subscription has a preset assigned
    if sub.preset_id:
        preset = SubscriptionPreset.query.get(sub.preset_id)
        if preset and preset.is_active:
            original_count = len(all_uris)
            filtered_uris = []
            for uri in all_uris:
                # Extract config name from URI (part after #)
                config_name = ""
                if '#' in uri:
                    config_name = uri.split('#')[-1]
                    # URL decode the name
                    try:
                        config_name = urllib.parse.unquote(config_name)
                    except Exception:
                        pass
                
                if preset.matches_config(config_name):
                    filtered_uris.append(uri)
                else:
                    logger.debug(f"Filtered out config '{config_name}' by preset '{preset.name}'")
            
            all_uris = filtered_uris
            logger.info(f"Preset '{preset.name}' filtered configs: {original_count} -> {len(all_uris)}")
    
    if not all_uris:
        return "No active configurations found", 404
    
    # Calculate expiry timestamp for happ format
    from datetime import timedelta
    
    expire_timestamp = 0
    if sub.expiry_days > 0:
        expiry_date = sub.created_at + timedelta(days=sub.expiry_days)
        expire_timestamp = int(expiry_date.timestamp())
    
    # Collect traffic stats from all panels
    total_up = 0
    total_down = 0
    for panel in xui_client.panels:
        try:
            panel.login()
            traffic = panel.get_client_traffic_by_uuid(sub.uuid)
            total_up += traffic.get('upload', 0)
            total_down += traffic.get('download', 0)
        except Exception:
            pass
    
    def _utf8_header(value: str) -> str:
        """Encode UTF-8 string so it passes through WSGI latin-1 headers.
        Gunicorn will encode back to bytes, producing valid UTF-8 for the client."""
        return value.encode('utf-8').decode('latin-1')

    # Happ headers: traffic in bytes!
    total_bytes = int((sub.total_gb or 0) * 1024 * 1024 * 1024)
    sub_info = f"upload={total_up}; download={total_down}; total={total_bytes}; expire={expire_timestamp}"
    profile_title = (gsettings.sub_title or sub.email)[:25]  # happ limit: 25 chars
    
    headers = {
        'Content-Type': 'text/plain; charset=utf-8',
        'profile-title': _utf8_header(profile_title),
        'subscription-userinfo': sub_info,
        'profile-update-interval': '1',
    }
    if gsettings.sub_description:
        # Happ uses sub-info-text for subscription description block
        headers['sub-info-text'] = _utf8_header(gsettings.sub_description[:200])
        headers['sub-info-color'] = 'blue'
    
    # Happ metadata headers
    if gsettings.sub_expire_enabled:
        headers['sub-expire'] = '1'
    if gsettings.sub_expire_button_link:
        headers['sub-expire-button-link'] = gsettings.sub_expire_button_link
    if gsettings.sub_info_button_text:
        headers['sub-info-button-text'] = _utf8_header(gsettings.sub_info_button_text[:25])
    if gsettings.sub_info_button_link:
        headers['sub-info-button-link'] = gsettings.sub_info_button_link
    if gsettings.announce_text:
        headers['announce'] = _utf8_header(gsettings.announce_text[:200])
    if gsettings.fallback_url:
        headers['fallback-url'] = gsettings.fallback_url
    if gsettings.profile_web_page_url:
        headers['profile-web-page-url'] = gsettings.profile_web_page_url
    if gsettings.support_url:
        headers['support-url'] = gsettings.support_url
    
    # Happ custom routing (custom-tunnel-config)
    if gsettings.happ_routing_enabled:
        routing_config = gsettings.happ_routing_config
        if not routing_config:
            # Default RoscomVPN JSONSUB configuration
            routing_config = '{"Name":"RoscomVPN","GlobalProxy":"true","UseChunkFiles":"false","RemoteDns":"8.8.8.8","DomesticDns":"77.88.8.8","RemoteDNSType":"DoH","RemoteDNSDomain":"https://8.8.8.8/dns-query","RemoteDNSIP":"8.8.8.8","DomesticDNSType":"DoH","DomesticDNSDomain":"https://77.88.8.8/dns-query","DomesticDNSIP":"77.88.8.8","Geoipurl":"https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geoip@202605020543/release/geoip.dat","Geositeurl":"https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geosite@202604152235/release/geosite.dat","RouteOrder":"block-proxy-direct","DirectSites":[],"DirectIp":[],"ProxySites":[],"ProxyIp":[],"BlockSites":[],"BlockIp":[],"DomainStrategy":"IPIfNonMatch","FakeDNS":"false"}'
        headers['custom-tunnel-config'] = _utf8_header(routing_config)
    
    # Return based on format
    if is_clash:
        # Build full Clash YAML with proxies, groups, and rules
        proxies = []
        for panel in xui_client.panels:
            try:
                panel.login()
                inbounds = panel.get_inbounds()
                for inbound in inbounds:
                    settings_str = inbound.get('settings', '{}')
                    try:
                        settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                        clients = settings.get('clients', [])
                        for client in clients:
                            if client.get('id') == sub.uuid:
                                remark = f"{panel.config.name}-{sub.email}"
                                protocol = inbound.get('protocol', 'vless')
                                listen = inbound.get('listen', '0.0.0.0')
                                port = inbound.get('port', 443)
                                # Use panel's sub_host if set, otherwise use inbound listen
                                host = panel.config.sub_host or panel.config.host
                                if listen == '0.0.0.0':
                                    listen = host
                                proxy = _build_clash_proxy(remark, protocol, listen, port, client, inbound.get('streamSettings', '{}'))
                                proxies.append(proxy)
                                break
                    except Exception:
                        continue
            except Exception:
                continue
        
        if not proxies:
            return "No active configurations found", 404
        
        yaml_text = _build_clash_yaml(proxies, gsettings.custom_rules or '')
        headers['Content-Type'] = 'text/yaml; charset=utf-8'
        return yaml_text, 200, headers
    else:
        # Base64-encoded URI list (standard for v2rayN/Shadowrocket/happ)
        uri_text = '\n'.join(all_uris)
        encoded = base64.b64encode(uri_text.encode()).decode()
        return encoded, 200, headers


def _extract_host(url: str) -> str:
    """Extract hostname from panel URL."""
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname or parsed.path.split('/')[0].split(':')[0]


def _build_vless_uri(host, port, uuid, remark, flow, stream_settings_str):
    """Build vless:// URI from inbound settings."""
    try:
        stream = json.loads(stream_settings_str) if isinstance(stream_settings_str, str) else stream_settings_str or {}
    except json.JSONDecodeError:
        stream = {}
    
    params = {}
    network = stream.get('network', 'tcp')
    if network:
        params['type'] = network
    
    security = stream.get('security', 'none')
    if security and security != 'none':
        params['security'] = security
    
    if flow:
        params['flow'] = flow
    
    # Reality settings - try different field names that 3x-ui might use
    reality = stream.get('realitySettings', {})
    if reality:
        # Try different possible field names for public key
        pbk = (reality.get('publicKey') or 
               reality.get('pubKey') or 
               reality.get('pbk') or '')
        params['pbk'] = pbk
        params['fp'] = reality.get('fingerprint', 'chrome')
        # Try different field names for server names
        server_names = (reality.get('serverNames') or 
                       reality.get('dest') or 
                       reality.get('serverName') or [])
        if isinstance(server_names, str):
            server_names = [server_names]
        params['sni'] = server_names[0] if server_names else ''
        # Try different field names for short ID
        short_ids = reality.get('shortIds') or reality.get('shortId') or []
        if isinstance(short_ids, str):
            short_ids = [short_ids]
        params['sid'] = short_ids[0] if short_ids else ''
        if not pbk:
            logger.warning(f"Empty publicKey for Reality config: {host}:{port} - check 3x-ui panel settings")
        logger.debug(f"Reality params: pbk={bool(pbk)}, sni={params.get('sni')}, sid={params.get('sid')}")
    
    # TLS settings  
    tls = stream.get('tlsSettings', {})
    if tls:
        sni = tls.get('serverName', '')
        if sni:
            params['sni'] = sni
    
    query = urllib.parse.urlencode(params)
    name = urllib.parse.quote(remark)
    
    return f"vless://{uuid}@{host}:{port}?{query}#{name}"


RUSSIAN_SERVICES_RULES = [
    'DOMAIN-SUFFIX,yandex.ru,DIRECT',
    'DOMAIN-SUFFIX,yandex.net,DIRECT',
    'DOMAIN-SUFFIX,ya.ru,DIRECT',
    'DOMAIN-SUFFIX,vk.com,DIRECT',
    'DOMAIN-SUFFIX,vk.ru,DIRECT',
    'DOMAIN-SUFFIX,ok.ru,DIRECT',
    'DOMAIN-SUFFIX,mail.ru,DIRECT',
    'DOMAIN-SUFFIX,avito.ru,DIRECT',
    'DOMAIN-SUFFIX,avito.net,DIRECT',
    'DOMAIN-SUFFIX,ozon.ru,DIRECT',
    'DOMAIN-SUFFIX,wildberries.ru,DIRECT',
    'DOMAIN-SUFFIX,wb.ru,DIRECT',
    'DOMAIN-SUFFIX,sberbank.ru,DIRECT',
    'DOMAIN-SUFFIX,sber.ru,DIRECT',
    'DOMAIN-SUFFIX,tinkoff.ru,DIRECT',
    'DOMAIN-SUFFIX,vtb.ru,DIRECT',
    'DOMAIN-SUFFIX,alfabank.ru,DIRECT',
    'DOMAIN-SUFFIX,gosuslugi.ru,DIRECT',
    'DOMAIN-SUFFIX,nalog.ru,DIRECT',
    'DOMAIN-SUFFIX,pfr.gov.ru,DIRECT',
    'DOMAIN-SUFFIX,mos.ru,DIRECT',
    'DOMAIN-SUFFIX,spb.ru,DIRECT',
    'DOMAIN-SUFFIX,2gis.ru,DIRECT',
    'DOMAIN-SUFFIX,kontur.ru,DIRECT',
    'DOMAIN-SUFFIX,skbkontur.ru,DIRECT',
    'DOMAIN-SUFFIX,gismeteo.ru,DIRECT',
    'DOMAIN-SUFFIX,kinopoisk.ru,DIRECT',
    'DOMAIN-SUFFIX,hd.kinopoisk.ru,DIRECT',
    'DOMAIN-SUFFIX,ivi.ru,DIRECT',
    'DOMAIN-SUFFIX,mts.ru,DIRECT',
    'DOMAIN-SUFFIX,megafon.ru,DIRECT',
    'DOMAIN-SUFFIX,beeline.ru,DIRECT',
    'DOMAIN-SUFFIX,tele2.ru,DIRECT',
    'DOMAIN-SUFFIX,rt.ru,DIRECT',
    'DOMAIN-SUFFIX,gosuslugi.ru,DIRECT',
    'DOMAIN-SUFFIX,mos.ru,DIRECT',
    'GEOIP,RU,DIRECT',
    'GEOSITE,category-ru,DIRECT',
]


def _build_clash_proxy(name, protocol, host, port, client, stream_settings_str):
    """Build Clash proxy config dict."""
    try:
        stream = json.loads(stream_settings_str) if isinstance(stream_settings_str, str) else stream_settings_str or {}
    except json.JSONDecodeError:
        stream = {}
    
    proxy = {
        'name': name,
        'type': protocol,
        'server': host,
        'port': port,
        'uuid': client.get('id'),
    }
    
    if client.get('flow'):
        proxy['flow'] = client['flow']
    
    security = stream.get('security', 'none')
    if security in ('tls', 'reality', 'xtls'):
        proxy['tls'] = True
    
    network = stream.get('network', 'tcp')
    if network == 'ws':
        proxy['network'] = 'ws'
        ws = stream.get('wsSettings', {})
        if ws.get('path'):
            proxy['ws-opts'] = {'path': ws['path']}
        if ws.get('headers', {}).get('Host'):
            proxy['ws-opts'] = proxy.get('ws-opts', {})
            proxy['ws-opts']['headers'] = {'Host': ws['headers']['Host']}
    elif network == 'grpc':
        proxy['network'] = 'grpc'
        grpc = stream.get('grpcSettings', {})
        if grpc.get('serviceName'):
            proxy['grpc-opts'] = {'grpc-service-name': grpc['serviceName']}
    
    # Reality/TLS SNI
    reality = stream.get('realitySettings', {})
    tls = stream.get('tlsSettings', {})
    sni = (reality.get('serverNames', [''])[0] if reality.get('serverNames') else '') or tls.get('serverName', '')
    if sni:
        proxy['servername'] = sni
    
    return proxy


def _build_clash_yaml(proxies: List[dict], custom_rules: str = '') -> str:
    """Build full Clash YAML config with proxies, proxy-groups, and rules."""
    proxy_names = [p['name'] for p in proxies]
    
    lines = ['proxies:']
    for p in proxies:
        lines.append(f'  - name: {p["name"]}')
        lines.append(f'    type: {p["type"]}')
        lines.append(f'    server: {p["server"]}')
        lines.append(f'    port: {p["port"]}')
        lines.append(f'    uuid: {p["uuid"]}')
        if 'flow' in p:
            lines.append(f'    flow: {p["flow"]}')
        if p.get('tls'):
            lines.append('    tls: true')
        if 'network' in p:
            lines.append(f'    network: {p["network"]}')
        if 'ws-opts' in p:
            lines.append('    ws-opts:')
            for k, v in p['ws-opts'].items():
                if isinstance(v, dict):
                    lines.append(f'      {k}:')
                    for kk, vv in v.items():
                        lines.append(f'        {kk}: {vv}')
                else:
                    lines.append(f'      {k}: {v}')
        if 'grpc-opts' in p:
            lines.append('    grpc-opts:')
            for k, v in p['grpc-opts'].items():
                lines.append(f'      {k}: {v}')
        if 'servername' in p:
            lines.append(f'    servername: {p["servername"]}')
    
    lines.append('')
    lines.append('proxy-groups:')
    lines.append('  - name: Proxy')
    lines.append('    type: select')
    lines.append('    proxies:')
    for name in proxy_names:
        lines.append(f'      - {name}')
    lines.append('      - DIRECT')
    
    lines.append('')
    lines.append('rules:')
    
    # Custom rules first (highest priority)
    if custom_rules:
        for rule in custom_rules.strip().split('\n'):
            rule = rule.strip()
            if rule and not rule.startswith('#'):
                lines.append(f'  - {rule}')
    
    # Russian services bypass
    for rule in RUSSIAN_SERVICES_RULES:
        lines.append(f'  - {rule}')
    
    lines.append('  - MATCH,Proxy')
    
    return '\n'.join(lines)


SUBSCRIPTION_GUIDE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Подписка VPN - {{ email }}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 20px auto; padding: 20px; background: #1a1a2e; color: #eee; line-height: 1.6; }
        h1 { color: #00d4ff; margin-bottom: 10px; }
        h2 { color: #00d4ff; margin-top: 30px; border-bottom: 2px solid #00d4ff; padding-bottom: 10px; }
        h3 { color: #00d4ff; margin-top: 20px; }
        .card { background: #16213e; border-radius: 12px; padding: 20px; margin: 15px 0; }
        .link { background: #0f3460; padding: 15px; border-radius: 8px; word-break: break-all; font-family: monospace; font-size: 14px; border: 1px solid #00d4ff; }
        button { background: #00d4ff; color: #1a1a2e; border: none; padding: 12px 24px; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 16px; }
        button:hover { background: #33ddff; }
        .btn-success { background: #28a745; color: white; }
        .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0; }
        .tab { background: #0f3460; padding: 10px 20px; border-radius: 20px; cursor: pointer; border: 2px solid transparent; }
        .tab:hover { background: #1a4a7a; }
        .tab.active { border-color: #00d4ff; background: #00d4ff; color: #1a1a2e; font-weight: bold; }
        .platform-content { display: none; }
        .platform-content.active { display: block; }
        .step { display: flex; align-items: flex-start; margin: 15px 0; }
        .step-num { background: #00d4ff; color: #1a1a2e; width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; margin-right: 12px; flex-shrink: 0; margin-top: 2px; }
        .step-content { flex: 1; }
        .note { background: #2d4a6a; padding: 10px 15px; border-radius: 8px; border-left: 4px solid #00d4ff; margin: 15px 0; }
        .warning { background: #5a4a2a; padding: 10px 15px; border-radius: 8px; border-left: 4px solid #ffc107; margin: 15px 0; }
        a { color: #00d4ff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .recommendation { color: #ffc107; font-weight: bold; }
        ul { margin: 10px 0; padding-left: 25px; }
        li { margin: 8px 0; }
        .header-info { font-size: 14px; color: #aaa; margin-bottom: 20px; }
    </style>
</head>
<body>
    <h1>📡 Подписка VPN</h1>
    <p class="header-info"><strong>Пользователь:</strong> {{ email }}</p>
    
    <div class="card">
        <h3>🔗 Ваша ссылка подписки:</h3>
        <div class="link" id="sub-link">{{ sub_url }}</div>
        <br>
        <button id="copy-btn" onclick="copyLink()">📋 Копировать ссылку</button>
        <p style="font-size: 12px; color: #888; margin-top: 10px;">Нажмите кнопку, чтобы скопировать ссылку для импорта в клиент</p>
    </div>
    
    <h2>📱 Выберите платформу</h2>
    <div class="tabs">
        <div class="tab active" onclick="showPlatform('android')">Android</div>
        <div class="tab" onclick="showPlatform('ios')">iOS</div>
        <div class="tab" onclick="showPlatform('windows')">Windows</div>
        <div class="tab" onclick="showPlatform('macos')">macOS</div>
        <div class="tab" onclick="showPlatform('linux')">Linux</div>
        <div class="tab" onclick="showPlatform('openwrt')">OpenWRT</div>
    </div>
    
    <!-- Android -->
    <div id="android" class="platform-content active">
        <div class="card">
            <h3>📱 Android</h3>
            
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-content">
                    <strong>Скачайте клиент</strong><br>
                    Мы настоятельно рекомендуем <span class="recommendation">Happ</span> — скачайте из <a href="https://play.google.com/store/apps/details?id=com.happProxy" target="_blank">Google Play</a> или <a href="https://github.com/Happ-proxy/happ-android/releases" target="_blank">GitHub</a>.<br>
                    Также можно использовать любой другой клиент с поддержкой VLESS/Vmess: V2rayNG, Clash Meta for Android.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-content">
                    <strong>Настройка клиента</strong><br>
                    Happ по умолчанию уже отлично настроен. Если настройки сломались, их можно откатить: шестеренка → листаем вниз → красная кнопка "Сброс" → сброс настроек.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-content">
                    <strong>Импортируйте подписку</strong><br>
                    Скопируйте ссылку подписки (кнопка выше), затем в Happ: справа вверху плюсик → "Вставить из буфера обмена".
                </div>
            </div>
            
            <div class="note">
                <strong>💡 Рекомендации:</strong><br>
                • Обновляйте подписку — клиент делает это автоматически, но если что-то не работает, обновите вручную прежде чем писать в поддержку.<br>
                • Не все конфиги доступны разом — это особенность нашего подхода. Есть несколько конфигов под разные задачи, и в разных условиях сети некоторые могут быть недоступны.<br>
                • Для проверки доступности: справа от названия подписки нажмите кнопку спидометра. Клиент покажет пинг (мс) или "н/д" (не доступно).
            </div>
        </div>
    </div>
    
    <!-- iOS -->
    <div id="ios" class="platform-content">
        <div class="card">
            <h3>🍎 iOS / iPadOS</h3>
            
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-content">
                    <strong>Скачайте клиент</strong><br>
                    Рекомендуем <span class="recommendation">Happ</span> из <a href="https://apps.apple.com/app/happ-proxy-utility/id6504287215" target="_blank">App Store</a> (нужен Apple ID другого региона) или <a href="https://testflight.apple.com/join/..." target="_blank">TestFlight</a>.<br>
                    Альтернативы: Streisand, Shadowrocket (если доступен в вашем регионе).
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-content">
                    <strong>Импорт подписки</strong><br>
                    Скопируйте ссылку подписки (кнопка выше). В Happ нажмите "+" вверху → "Добавить подписку" → вставьте URL.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-content">
                    <strong>Включите VPN</strong><br>
                    Нажмите на переключатель рядом с подпиской. При первом подключении система попросит разрешение — нажмите "Разрешить".
                </div>
            </div>
            
            <div class="note">
                <strong>💡 Совет:</strong> На iOS для стабильной работы рекомендуем использовать Happ с включённым "Include all networks" в настройках TUN (iOS 16.4+).
            </div>
        </div>
    </div>
    
    <!-- Windows -->
    <div id="windows" class="platform-content">
        <div class="card">
            <h3>🪟 Windows</h3>
            
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-content">
                    <strong>Скачайте клиент</strong><br>
                    Рекомендуем <span class="recommendation">Happ</span> — скачайте с <a href="https://github.com/Happ-proxy/happ-desktop/releases" target="_blank">GitHub Releases</a>.<br>
                    Альтернативы: v2rayN, Clash Verge, Clash Verge Rev, Hiddify.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-content">
                    <strong>Импорт подписки</strong><br>
                    Happ сразу предложит импортировать подписку при первом запуске. Просто следуйте инструкциям.<br>
                    Если нужно импортировать ещё раз: слева вверху плюсик в квадрате → "Subscription name": любое название → "Subscription URL": вставьте URL подписки.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-content">
                    <strong>Настройка клиента (важно!)</strong><br>
                    В главном меню выберите режим <strong>TUN</strong> вместо Proxy.<br>
                    Затем: Settings → Advanced Settings → Set system proxy: НЕТ, TUN: ДА.<br>
                    Если по какой-то причине что-то продолжает работать напрямую, а не через туннель — перезапустите программу. Она подтянет настройки TUN.
                </div>
            </div>
            
            <div class="warning">
                <strong>⚠️ Важно:</strong> На Windows режим Proxy работает только для приложений с поддержкой системного прокси. Для полной маршрутизации всего трафика используйте TUN режим.
            </div>
        </div>
    </div>
    
    <!-- macOS -->
    <div id="macos" class="platform-content">
        <div class="card">
            <h3>🍏 macOS</h3>
            
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-content">
                    <strong>Скачайте клиент</strong><br>
                    Рекомендуем <span class="recommendation">Happ</span> — скачайте с <a href="https://github.com/Happ-proxy/happ-desktop/releases" target="_blank">GitHub</a> (файл .dmg).<br>
                    Альтернативы: Clash Verge, ClashX.Meta, V2RayXS, Streisand (из Mac App Store).
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-content">
                    <strong>Установка</strong><br>
                    Откройте .dmg файл и перетащите Happ в Applications. При первом запуске может потребоваться разрешение в Системных настройках → Конфиденциальность и безопасность.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-content">
                    <strong>Импорт подписки</strong><br>
                    Скопируйте ссылку подписки. В Happ нажмите "+" → вставьте URL подписки → нажмите OK.
                </div>
            </div>
            
            <div class="note">
                <strong>💡 Совет:</strong> На macOS рекомендуем использовать Happ или Clash Verge в режиме TUN для максимальной совместимости со всеми приложениями.
            </div>
        </div>
    </div>
    
    <!-- Linux -->
    <div id="linux" class="platform-content">
        <div class="card">
            <h3>🐧 Linux</h3>
            
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-content">
                    <strong>Скачайте клиент</strong><br>
                    Рекомендуем <span class="recommendation">Happ</span> — скачайте AppImage с <a href="https://github.com/Happ-proxy/happ-desktop/releases" target="_blank">GitHub</a>.<br>
                    Альтернативы: Clash Verge Rev (AppImage), Hiddify, sing-box (CLI), v2rayA (Web UI).
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-content">
                    <strong>Запуск Happ (AppImage)</strong><br>
                    Сделайте файл исполняемым: <code>chmod +x happ-desktop-*.AppImage</code><br>
                    Запустите: <code>./happ-desktop-*.AppImage</code><br>
                    Или используйте <a href="https://appimage.github.io/AppImageLauncher/" target="_blank">AppImageLauncher</a> для интеграции в систему.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-content">
                    <strong>Импорт подписки</strong><br>
                    В Happ нажмите "+" в левом верхнем углу → введите название → вставьте URL подписки.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">4</div>
                <div class="step-content">
                    <strong>TUN режим (для всей системы)</strong><br>
                    В настройках Happ включите TUN режим. Это требует прав root — Happ попросит пароль sudo.<br>
                    Альтернатива: запустите Happ с правами sudo: <code>sudo ./happ-desktop-*.AppImage</code>
                </div>
            </div>
            
            <div class="note">
                <strong>💡 Для продвинутых пользователей:</strong><br>
                Можно использовать sing-box или Xray напрямую через CLI с конфигом, сконвертированным из подписки через <a href="https://v2rayse.com" target="_blank">v2rayse.com</a>.
            </div>
        </div>
    </div>
    
    <!-- OpenWRT -->
    <div id="openwrt" class="platform-content">
        <div class="card">
            <h3>📡 OpenWRT (роутер)</h3>
            
            <div class="step">
                <div class="step-num">1</div>
                <div class="step-content">
                    <strong>Установите необходимые пакеты</strong><br>
                    Через SSH на роутере выполните:<br>
                    <code>opkg update && opkg install sing-box v2ray-geoip v2ray-geosite</code><br>
                    Или для Xray: <code>opkg install xray-core</code>
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">2</div>
                <div class="step-content">
                    <strong>Конвертируйте подписку в конфиг</strong><br>
                    Подписка в формате base64 URI list. Используйте конвертер <a href="https://v2rayse.com" target="_blank">v2rayse.com</a> или скрипт:<br>
                    <code>echo 'ВАША_ССЫЛКА_BASE64' | base64 -d</code> получите URI, затем вставьте вручную в sing-box/Xray конфиг.
                </div>
            </div>
            
            <div class="step">
                <div class="step-num">3</div>
                <div class="step-content">
                    <strong>Настройка sing-box</strong><br>
                    Создайте конфиг <code>/etc/sing-box/config.json</code> с outbounds из ваших URI и routing rules для РФ (дописывайте direct для geosite:ru, geoip:ru).<br>
                    Включите службу: <code>/etc/init.d/sing-box enable && /etc/init.d/sing-box start</code>
                </div>
            </div>
            
            <div class="warning">
                <strong>⚠️ Требуется опыт:</strong> Настройка VPN на роутере требует понимания сетей. При неправильной конфигурации вы потеряете доступ к роутеру.
            </div>
            
            <div class="note">
                <strong>📚 Рекомендуем:</strong> Используйте готовые решения с OpenWRT + sing-box:<br>
                • <a href="https://github.com/ophub/luci-app-sing-box" target="_blank">luci-app-sing-box</a> — Web UI для управления<br>
                • <a href="https://github.com/xiaorouji/openwrt-passwall" target="_blank">OpenWrt Passwall</a> — комплексное решение
            </div>
        </div>
    </div>
    
    <script>
        function showPlatform(platform) {
            // Hide all
            document.querySelectorAll('.platform-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
            
            // Show selected
            document.getElementById(platform).classList.add('active');
            event.target.classList.add('active');
        }
        
        function copyLink() {
            const linkText = document.getElementById('sub-link').innerText;
            const btn = document.getElementById('copy-btn');
            
            // Try modern clipboard API first
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(linkText).then(function() {
                    showSuccess(btn);
                }, function(err) {
                    fallbackCopy(linkText, btn);
                });
            } else {
                fallbackCopy(linkText, btn);
            }
        }
        
        function fallbackCopy(text, btn) {
            // Create temporary textarea
            const textArea = document.createElement('textarea');
            textArea.value = text;
            textArea.style.position = 'fixed';
            textArea.style.left = '-999999px';
            textArea.style.top = '-999999px';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            
            try {
                const successful = document.execCommand('copy');
                if (successful) {
                    showSuccess(btn);
                } else {
                    alert('Не удалось скопировать. Пожалуйста, скопируйте вручную.');
                }
            } catch (err) {
                alert('Не удалось скопировать. Пожалуйста, скопируйте вручную.');
            }
            
            document.body.removeChild(textArea);
        }
        
        function showSuccess(btn) {
            const originalText = btn.innerText;
            btn.innerText = '✅ Скопировано!';
            btn.classList.add('btn-success');
            setTimeout(function() {
                btn.innerText = originalText;
                btn.classList.remove('btn-success');
            }, 2000);
        }
    </script>
</body>
</html>
"""


@app.route('/settings', methods=['GET', 'POST'])
@require_auth
def global_settings():
    """Глобальные настройки подписок для всех пользователей."""
    settings = GlobalSettings.get()
    
    if request.method == 'POST':
        settings.sub_title = request.form.get('sub_title', 'VPN Subscription')
        settings.sub_description = request.form.get('sub_description', '')
        settings.default_total_gb = float(request.form.get('default_total_gb', 0) or 0)
        settings.default_expiry_days = int(request.form.get('default_expiry_days', 30) or 30)
        settings.custom_rules = request.form.get('custom_rules', '')
        settings.custom_direct_countries = request.form.get('custom_direct_countries', '')
        settings.auto_sync_enabled = request.form.get('auto_sync_enabled') == 'on'
        settings.auto_sync_interval_minutes = int(request.form.get('auto_sync_interval_minutes', 30) or 30)
        
        # Happ metadata
        settings.sub_expire_enabled = request.form.get('sub_expire_enabled') == 'on'
        settings.sub_expire_button_link = request.form.get('sub_expire_button_link', '')
        settings.sub_info_button_text = request.form.get('sub_info_button_text', '')
        settings.sub_info_button_link = request.form.get('sub_info_button_link', '')
        settings.announce_text = request.form.get('announce_text', '')
        settings.fallback_url = request.form.get('fallback_url', '')
        settings.profile_web_page_url = request.form.get('profile_web_page_url', '')
        settings.support_url = request.form.get('support_url', '')
        
        # Happ routing
        settings.happ_routing_enabled = request.form.get('happ_routing_enabled') == 'on'
        settings.happ_routing_config = request.form.get('happ_routing_config', '')
        
        db.session.commit()
        return redirect('/settings')
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head><title>Global Settings - 3x-controller</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; max-width: 800px; }
        h1 { color: #333; }
        .nav { margin: 20px 0; }
        .nav a { margin-right: 20px; text-decoration: none; color: #007bff; }
        .form-group { margin: 15px 0; }
        label { display: block; font-weight: bold; margin-bottom: 5px; }
        input[type="text"], input[type="number"], textarea {
            width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px;
        }
        textarea { min-height: 100px; font-family: monospace; }
        button { padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .help { font-size: 12px; color: #666; margin-top: 4px; }
    </style>
    </head>
    <body>
        <h1>Global Subscription Settings</h1>
        <div class="nav">
            <a href="/">Dashboard</a>
            <a href="/subscriptions">Subscriptions</a>
            <a href="/presets">Presets</a>
            <a href="/logout" style="float:right;">Logout</a>
        </div>
        
        <form method="POST">
            <div class="form-group">
                <label>Subscription Title:</label>
                <input type="text" name="sub_title" value="{{ settings.sub_title or 'VPN Subscription' }}">
                <div class="help">Название подписки для отображения в клиентах</div>
            </div>
            
            <div class="form-group">
                <label>Description:</label>
                <input type="text" name="sub_description" value="{{ settings.sub_description or '' }}">
                <div class="help">Описание подписки</div>
            </div>
            
            <div class="form-group">
                <label>Default Traffic Limit (GB):</label>
                <input type="number" name="default_total_gb" value="{{ settings.default_total_gb or 0 }}" step="0.1">
                <div class="help">0 = без ограничений. Применяется для новых подписок.</div>
            </div>
            
            <div class="form-group">
                <label>Default Expiry Days:</label>
                <input type="number" name="default_expiry_days" value="{{ settings.default_expiry_days or 30 }}">
                <div class="help">Дней до истечения для новых подписок</div>
            </div>
            
            <div class="form-group">
                <label>Custom Clash Rules:</label>
                <textarea name="custom_rules">{{ settings.custom_rules or '' }}</textarea>
                <div class="help">Каждое правило с новой строки. Пример: "DOMAIN-SUFFIX,google.com,DIRECT" или "IP-CIDR,192.168.0.0/16,DIRECT"</div>
            </div>
            
            <div class="form-group">
                <label>Direct Countries (comma-separated):</label>
                <input type="text" name="custom_direct_countries" value="{{ settings.custom_direct_countries or '' }}">
                <div class="help">Коды стран для прямого маршрута (RU,BY,KZ,...). Пока не используется.</div>
            </div>
            
            <div class="form-group">
                <label>
                    <input type="checkbox" name="auto_sync_enabled" {% if settings.auto_sync_enabled %}checked{% endif %}>
                    Enable Auto-Sync with Panels
                </label>
                <div class="help">Автоматически синхронизировать все подписки с 3x-ui панелями</div>
            </div>
            
            <div class="form-group">
                <label>Auto-Sync Interval (minutes):</label>
                <input type="number" name="auto_sync_interval_minutes" value="{{ settings.auto_sync_interval_minutes or 30 }}" min="5">
                <div class="help">Минимум 5 минут между синхронизациями</div>
            </div>
            
            <hr style="margin: 30px 0;">
            <h3>Happ Subscription Metadata</h3>
            
            <div class="form-group">
                <label>
                    <input type="checkbox" name="sub_expire_enabled" {% if settings.sub_expire_enabled %}checked{% endif %}>
                    Enable Subscription Expire Notifications
                </label>
                <div class="help">Показывать уведомления об истечении подписки (3 дня до и после)</div>
            </div>
            
            <div class="form-group">
                <label>Expire Button Link:</label>
                <input type="text" name="sub_expire_button_link" value="{{ settings.sub_expire_button_link or '' }}">
                <div class="help">Ссылка для кнопки "Renew" при истечении (Telegram, URL)</div>
            </div>
            
            <div class="form-group">
                <label>Info Button Text:</label>
                <input type="text" name="sub_info_button_text" value="{{ settings.sub_info_button_text or '' }}" maxlength="25">
                <div class="help">Текст кнопки в info-блоке (макс. 25 символов)</div>
            </div>
            
            <div class="form-group">
                <label>Info Button Link:</label>
                <input type="text" name="sub_info_button_link" value="{{ settings.sub_info_button_link or '' }}">
                <div class="help">Ссылка для info-кнопки (поддержка, чат)</div>
            </div>
            
            <div class="form-group">
                <label>Announce Text:</label>
                <textarea name="announce_text">{{ settings.announce_text or '' }}</textarea>
                <div class="help">Всплывающее уведомление при обновлении подписки (до 200 символов или base64)</div>
            </div>
            
            <div class="form-group">
                <label>Profile Web Page URL:</label>
                <input type="text" name="profile_web_page_url" value="{{ settings.profile_web_page_url or '' }}">
                <div class="help">Ссылка на сайт/канал, отображается в профиле подписки</div>
            </div>
            
            <div class="form-group">
                <label>Support URL:</label>
                <input type="text" name="support_url" value="{{ settings.support_url or '' }}">
                <div class="help">Ссылка на поддержку (отображается как кнопка "?")</div>
            </div>
            
            <div class="form-group">
                <label>Fallback URL:</label>
                <input type="text" name="fallback_url" value="{{ settings.fallback_url or '' }}">
                <div class="help">Резервный URL подписки если основной недоступен</div>
            </div>
            
            <hr style="margin: 30px 0;">
            <h3>Happ Routing (чтобы IP нод не попадал в черный список)</h3>
            
            <div class="form-group">
                <label>
                    <input type="checkbox" name="happ_routing_enabled" {% if settings.happ_routing_enabled %}checked{% endif %}>
                    Enable Happ Custom Routing
                </label>
                <div class="help">Передавать routing конфигурацию через custom-tunnel-config. Гос. сервисы пойдут мимо туннеля.</div>
            </div>
            
            <div class="form-group">
                <label>Custom Routing JSON:</label>
                <textarea name="happ_routing_config" style="min-height: 200px;">{{ settings.happ_routing_config or '' }}</textarea>
                <div class="help">JSON конфигурация для Happ routing. Оставьте пустым для использования RoscomVPN JSONSUB по умолчанию. Генератор: <a href="https://routing.happ.su" target="_blank">routing.happ.su</a></div>
            </div>
            
            <button type="submit">Save Settings</button>
        </form>
    </body>
    </html>
    """, settings=settings)


@app.route('/api/settings', methods=['GET', 'PUT'])
@require_auth
def api_settings():
    """REST API для глобальных настроек."""
    settings = GlobalSettings.get()
    
    if request.method == 'PUT':
        data = request.get_json() or {}
        if 'sub_title' in data:
            settings.sub_title = data['sub_title']
        if 'sub_description' in data:
            settings.sub_description = data['sub_description']
        if 'default_total_gb' in data:
            settings.default_total_gb = float(data['default_total_gb'])
        if 'default_expiry_days' in data:
            settings.default_expiry_days = int(data['default_expiry_days'])
        if 'custom_rules' in data:
            settings.custom_rules = data['custom_rules']
        if 'custom_direct_countries' in data:
            settings.custom_direct_countries = data['custom_direct_countries']
        
        # Happ metadata
        if 'sub_expire_enabled' in data:
            settings.sub_expire_enabled = bool(data['sub_expire_enabled'])
        if 'sub_expire_button_link' in data:
            settings.sub_expire_button_link = data['sub_expire_button_link']
        if 'sub_info_button_text' in data:
            settings.sub_info_button_text = data['sub_info_button_text']
        if 'sub_info_button_link' in data:
            settings.sub_info_button_link = data['sub_info_button_link']
        if 'announce_text' in data:
            settings.announce_text = data['announce_text']
        if 'fallback_url' in data:
            settings.fallback_url = data['fallback_url']
        if 'profile_web_page_url' in data:
            settings.profile_web_page_url = data['profile_web_page_url']
        if 'support_url' in data:
            settings.support_url = data['support_url']
        
        # Happ routing
        if 'happ_routing_enabled' in data:
            settings.happ_routing_enabled = bool(data['happ_routing_enabled'])
        if 'happ_routing_config' in data:
            settings.happ_routing_config = data['happ_routing_config']
        
        db.session.commit()
        return jsonify({'success': True, 'settings': settings.to_dict()})
    
    return jsonify({'settings': settings.to_dict()})


# ==================== Preset Management UI ====================

@app.route('/presets')
@require_auth
def presets_ui():
    """Web UI for managing subscription presets."""
    presets = SubscriptionPreset.query.order_by(SubscriptionPreset.created_at.desc()).all()
    
    rows = ""
    for p in presets:
        status = "Active" if p.is_active else "Inactive"
        rows += f"""
        <tr>
            <td>{p.id}</td>
            <td>{p.name}</td>
            <td>{p.description or '-'}</td>
            <td>{p.include_patterns or '-'}</td>
            <td>{p.exclude_patterns or '-'}</td>
            <td>{status}</td>
            <td>
                <a href="/presets/{p.id}/edit" class="btn btn-small">Edit</a>
                <form method="POST" action="/presets/{p.id}/delete" style="display:inline;" 
                      onsubmit="return confirm('Delete preset {p.name}?')">
                    <button type="submit" class="btn btn-small btn-danger">Delete</button>
                </form>
            </td>
        </tr>
        """
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head><title>Presets - 3x-controller</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; }
        .nav { margin-bottom: 20px; }
        .nav a { text-decoration: none; color: #007bff; margin-right: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #f5f5f5; }
        .btn { padding: 5px 10px; background: #007bff; color: white; text-decoration: none; 
               border-radius: 3px; border: none; cursor: pointer; }
        .btn-danger { background: #dc3545; }
        .btn-small { font-size: 12px; }
        .new-btn { margin-bottom: 20px; display: inline-block; }
    </style>
    </head>
    <body>
        <div class="nav">
            <a href="/">Dashboard</a>
            <a href="/subscriptions">Subscriptions</a>
            <a href="/presets">Presets</a>
            <a href="/settings">Settings</a>
            <a href="/logout" style="float:right;">Logout</a>
        </div>
        <h1>Subscription Presets</h1>
        <a href="/presets/new" class="btn new-btn">Create New Preset</a>
        <table>
            <tr>
                <th>ID</th><th>Name</th><th>Description</th><th>Include</th><th>Exclude</th><th>Status</th><th>Actions</th>
            </tr>
            {{ rows|safe }}
        </table>
    </body>
    </html>
    """, rows=rows)


@app.route('/presets/new', methods=['GET', 'POST'])
@require_auth
def new_preset():
    """Create new preset UI."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        include_patterns = request.form.get('include_patterns', '').strip()
        exclude_patterns = request.form.get('exclude_patterns', '').strip()
        is_active = request.form.get('is_active') == 'on'
        
        if not name:
            return "Name is required", 400
        
        preset = SubscriptionPreset(
            name=name,
            description=description,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            is_active=is_active
        )
        db.session.add(preset)
        db.session.commit()
        
        logger.info(f"Created preset: {name}")
        return redirect('/presets')
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head><title>New Preset - 3x-controller</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; max-width: 600px; }
        .form-group { margin: 15px 0; }
        label { display: block; font-weight: bold; margin-bottom: 5px; }
        input, textarea { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
        button { padding: 10px 20px; background: #28a745; color: white; border: none; 
                 border-radius: 4px; cursor: pointer; }
        .nav { margin-bottom: 20px; }
        .nav a { text-decoration: none; color: #007bff; }
        .help { font-size: 12px; color: #666; margin-top: 4px; }
    </style>
    </head>
    <body>
        <div class="nav"><a href="/presets">&larr; Back to Presets</a></div>
        <h1>Create New Preset</h1>
        <form method="POST">
            <div class="form-group">
                <label>Name *</label>
                <input type="text" name="name" required>
            </div>
            <div class="form-group">
                <label>Description</label>
                <input type="text" name="description">
            </div>
            <div class="form-group">
                <label>Include Patterns (comma-separated)</label>
                <input type="text" name="include_patterns">
                <div class="help">e.g.: "stable,fast" - configs with these words in name will be included</div>
            </div>
            <div class="form-group">
                <label>Exclude Patterns (comma-separated)</label>
                <input type="text" name="exclude_patterns">
                <div class="help">e.g.: "test,debug" - configs with these words will be excluded</div>
            </div>
            <div class="form-group">
                <label><input type="checkbox" name="is_active" checked> Active</label>
            </div>
            <button type="submit">Create Preset</button>
        </form>
    </body>
    </html>
    """)


@app.route('/presets/<int:preset_id>/edit', methods=['GET', 'POST'])
@require_auth
def edit_preset(preset_id):
    """Edit preset UI."""
    preset = SubscriptionPreset.query.get_or_404(preset_id)
    
    if request.method == 'POST':
        preset.name = request.form.get('name', '').strip()
        preset.description = request.form.get('description', '').strip()
        preset.include_patterns = request.form.get('include_patterns', '').strip()
        preset.exclude_patterns = request.form.get('exclude_patterns', '').strip()
        preset.is_active = request.form.get('is_active') == 'on'
        
        db.session.commit()
        logger.info(f"Updated preset: {preset.name}")
        return redirect('/presets')
    
    checked = 'checked' if preset.is_active else ''
    return render_template_string(f"""
    <!DOCTYPE html>
    <html>
    <head><title>Edit Preset - 3x-controller</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; max-width: 600px; }}
        .form-group {{ margin: 15px 0; }}
        label {{ display: block; font-weight: bold; margin-bottom: 5px; }}
        input, textarea {{ width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
        button {{ padding: 10px 20px; background: #007bff; color: white; border: none; 
                 border-radius: 4px; cursor: pointer; }}
        .nav {{ margin-bottom: 20px; }}
        .nav a {{ text-decoration: none; color: #007bff; }}
        .help {{ font-size: 12px; color: #666; margin-top: 4px; }}
    </style>
    </head>
    <body>
        <div class="nav"><a href="/presets">&larr; Back to Presets</a></div>
        <h1>Edit Preset</h1>
        <form method="POST">
            <div class="form-group">
                <label>Name *</label>
                <input type="text" name="name" value="{preset.name}" required>
            </div>
            <div class="form-group">
                <label>Description</label>
                <input type="text" name="description" value="{preset.description or ''}">
            </div>
            <div class="form-group">
                <label>Include Patterns</label>
                <input type="text" name="include_patterns" value="{preset.include_patterns or ''}">
                <div class="help">Comma-separated patterns to include configs by name</div>
            </div>
            <div class="form-group">
                <label>Exclude Patterns</label>
                <input type="text" name="exclude_patterns" value="{preset.exclude_patterns or ''}">
                <div class="help">Comma-separated patterns to exclude configs by name</div>
            </div>
            <div class="form-group">
                <label><input type="checkbox" name="is_active" {checked}> Active</label>
            </div>
            <button type="submit">Update Preset</button>
        </form>
    </body>
    </html>
    """)


@app.route('/presets/<int:preset_id>/delete', methods=['POST'])
@require_auth
def delete_preset_ui(preset_id):
    """Delete preset UI action."""
    preset = SubscriptionPreset.query.get_or_404(preset_id)
    
    # Check if preset is in use
    if preset.subscriptions.count() > 0:
        return "Cannot delete preset that is in use by subscriptions", 409
    
    name = preset.name
    db.session.delete(preset)
    db.session.commit()
    
    logger.info(f"Deleted preset: {name}")
    return redirect('/presets')


# ==================== Preset Management API ====================

@app.route('/api/presets', methods=['GET'])
@require_auth
def list_presets():
    """List all subscription presets."""
    presets = SubscriptionPreset.query.all()
    return jsonify({
        'success': True,
        'count': len(presets),
        'presets': [p.to_dict() for p in presets]
    })


@app.route('/api/presets', methods=['POST'])
@require_auth
def create_preset():
    """Create a new subscription preset."""
    data = request.get_json() or {}
    
    name = data.get('name')
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    
    # Check for duplicate name
    if SubscriptionPreset.query.filter_by(name=name).first():
        return jsonify({'error': f'Preset with name "{name}" already exists'}), 409
    
    preset = SubscriptionPreset(
        name=name,
        description=data.get('description', ''),
        include_patterns=data.get('include_patterns', ''),
        exclude_patterns=data.get('exclude_patterns', ''),
        is_active=data.get('is_active', True)
    )
    
    db.session.add(preset)
    db.session.commit()
    
    logger.info(f"Created preset: {name}")
    
    return jsonify({
        'success': True,
        'preset': preset.to_dict()
    }), 201


@app.route('/api/presets/<int:preset_id>', methods=['GET'])
@require_auth
def get_preset(preset_id):
    """Get a specific preset by ID."""
    preset = SubscriptionPreset.query.get_or_404(preset_id)
    return jsonify({
        'success': True,
        'preset': preset.to_dict()
    })


@app.route('/api/presets/<int:preset_id>', methods=['PUT'])
@require_auth
def update_preset(preset_id):
    """Update a subscription preset."""
    preset = SubscriptionPreset.query.get_or_404(preset_id)
    data = request.get_json() or {}
    
    # Check name uniqueness if changing
    if 'name' in data and data['name'] != preset.name:
        if SubscriptionPreset.query.filter_by(name=data['name']).first():
            return jsonify({'error': f'Preset with name "{data["name"]}" already exists'}), 409
        preset.name = data['name']
    
    if 'description' in data:
        preset.description = data['description']
    if 'include_patterns' in data:
        preset.include_patterns = data['include_patterns']
    if 'exclude_patterns' in data:
        preset.exclude_patterns = data['exclude_patterns']
    if 'is_active' in data:
        preset.is_active = bool(data['is_active'])
    
    db.session.commit()
    
    logger.info(f"Updated preset: {preset.name}")
    
    return jsonify({
        'success': True,
        'preset': preset.to_dict()
    })


@app.route('/api/presets/<int:preset_id>', methods=['DELETE'])
@require_auth
def delete_preset(preset_id):
    """Delete a subscription preset."""
    preset = SubscriptionPreset.query.get_or_404(preset_id)
    
    # Check if preset is in use
    if preset.subscriptions.count() > 0:
        return jsonify({
            'error': 'Cannot delete preset that is in use by subscriptions'
        }), 409
    
    db.session.delete(preset)
    db.session.commit()
    
    logger.info(f"Deleted preset: {preset.name}")
    
    return jsonify({
        'success': True,
        'message': f'Preset {preset_id} ({preset.name}) deleted'
    })


# ==================== Sync API ====================

@app.route('/api/sync/all', methods=['POST'])
@require_auth
def sync_all_subscriptions():
    """Force sync all subscriptions with panels."""
    try:
        subs = Subscription.query.all()
        results = []
        
        for sub in subs:
            result = sync_service.sync_subscription(sub, 'update')
            results.append({
                'subscription_id': sub.id,
                'email': sub.email,
                'result': result
            })
        
        return jsonify({
            'success': True,
            'message': f'Scheduled sync for {len(results)} subscriptions',
            'results': results
        })
    except Exception as e:
        logger.exception("Failed to sync all subscriptions")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sync/<int:subscription_id>', methods=['POST'])
@require_auth
def sync_single_subscription(subscription_id):
    """Force sync specific subscription with panels."""
    try:
        sub = Subscription.query.get_or_404(subscription_id)
        result = sync_service.sync_subscription(sub, 'update')
        
        return jsonify({
            'success': True,
            'message': f'Synced subscription {subscription_id}',
            'result': result
        })
    except Exception as e:
        logger.exception(f"Failed to sync subscription {subscription_id}")
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
