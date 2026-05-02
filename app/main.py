"""Flask application for 3x-controller."""

import base64
import json
import logging
import os
import secrets
import urllib.parse
import uuid as uuid_lib
from datetime import datetime
from typing import List
from flask import Flask, jsonify, request, render_template_string, redirect, url_for

from app.xui_client import XUIClient
from app.models import db, Subscription, SyncLog, GlobalSettings
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
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        existing_tables = inspector.get_table_names()
        if 'subscriptions' not in existing_tables:
            db.create_all()
            logger.info("Database tables created")
        else:
            logger.info("Database tables already exist")
    except Exception:
        # Race condition: другой worker уже создал таблицы
        logger.info("Database tables likely created by another worker")

# Инициализация клиента 3x-ui
xui_client = XUIClient("config/panels.yaml")

# Инициализация сервиса синхронизации
sync_service = SyncService(xui_client)


@app.route('/')
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
            <a href="/settings">Settings</a>
            <a href="/api/health">API Health</a>
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
        
        rows += f"""
        <tr>
            <td>{sub.id}</td>
            <td>{sub.email}</td>
            <td>{sub.uuid or 'Auto'}</td>
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
            <a href="/subscriptions/new" class="btn">Create New</a>
            <button onclick="syncAll()" class="btn btn-orange" style="padding: 10px 20px; border: none; cursor: pointer; border-radius: 4px;">Sync All</button>
        </div>
        <table>
            <tr>
                <th>ID</th><th>Email</th><th>UUID</th><th>Traffic</th>
                <th>Expiry</th><th>Status</th><th>Sync</th><th>Sub</th><th>Actions</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>
    """)


@app.route('/subscriptions/new', methods=['GET', 'POST'])
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
        
        # Валидация
        if not email:
            return "Email is required", 400
        
        # Проверка уникальности email
        if Subscription.query.filter_by(email=email).first():
            return f"Subscription with email {email} already exists", 409
        
        # Создаем подписку
        sub = Subscription(
            email=email,
            uuid=uuid,
            sub_token=secrets.token_urlsafe(16),
            total_gb=total_gb,
            expiry_days=expiry_days,
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
    """, default_total_gb=settings.default_total_gb or 0, default_expiry_days=settings.default_expiry_days or 30)


@app.route('/subscriptions/<subscription_id>/edit', methods=['GET', 'POST'])
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
        sub.expiry_days = int(request.form.get('expiry_days', sub.expiry_days))
        sub.enabled = request.form.get('enabled') == 'on'
        sub.flow = request.form.get('flow', sub.flow)
        sub.sync_status = 'pending'
        sub.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        # Запускаем синхронизацию
        sync_service.schedule_sync(sub, 'update')
        
        return redirect(url_for('list_subscriptions'))
    
    # GET - форма редактирования со всеми полями
    checked = 'checked' if sub.enabled else ''
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
    """)


@app.route('/subscriptions/<subscription_id>/delete', methods=['POST'])
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
    sub = Subscription(
        email=email,
        uuid=uuid_val if uuid_val else str(uuid_lib.uuid4()),
        sub_token=secrets.token_urlsafe(16),
        total_gb=float(data.get('total_gb', 0)),
        expiry_days=int(data.get('expiry_days', 0)),
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
def list_subscriptions_api():
    """Список всех подписок (REST API)."""
    subs = Subscription.query.order_by(Subscription.created_at.desc()).all()
    
    return jsonify({
        'success': True,
        'count': len(subs),
        'subscriptions': [s.to_dict() for s in subs]
    })


@app.route('/api/panels', methods=['GET'])
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
            
            # Use saved sub_id if available
            if sub.sub_id:
                client_sub_id = sub.sub_id
                logger.debug(f"Panel {panel.config.name}: using saved sub_id={client_sub_id}")
            else:
                # Find client sub_id on this panel by UUID
                inbounds = panel.get_inbounds()
                for inbound in inbounds:
                    settings_str = inbound.get('settings', '{}')
                    try:
                        settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                        clients = settings.get('clients', [])
                        for client in clients:
                            if client.get('id') == sub.uuid:
                                client_sub_id = client.get('subId') or client.get('id')
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
    
    # Happ headers: traffic in bytes!
    total_bytes = int((sub.total_gb or 0) * 1024 * 1024 * 1024)
    sub_info = f"upload={total_up}; download={total_down}; total={total_bytes}; expire={expire_timestamp}"
    profile_title = (gsettings.sub_title or sub.email)[:25]  # happ limit: 25 chars
    
    headers = {
        'Content-Type': 'text/plain; charset=utf-8',
        'profile-title': profile_title,
        'subscription-userinfo': sub_info,
        'profile-update-interval': '1',
    }
    if gsettings.sub_description:
        headers['profile-description'] = gsettings.sub_description[:200]
    
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
    <title>Subscription Guide</title>
    <style>
        body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background: #1a1a2e; color: #eee; }
        h1 { color: #00d4ff; }
        .card { background: #16213e; border-radius: 12px; padding: 20px; margin: 15px 0; }
        .link { background: #0f3460; padding: 12px; border-radius: 8px; word-break: break-all; font-family: monospace; font-size: 14px; }
        button { background: #00d4ff; color: #1a1a2e; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        .step { display: flex; align-items: center; margin: 10px 0; }
        .step-num { background: #00d4ff; color: #1a1a2e; width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; margin-right: 12px; flex-shrink: 0; }
    </style>
</head>
<body>
    <h1>VPN Subscription</h1>
    <div class="card">
        <p><strong>User:</strong> {{ email }}</p>
        <p>Your subscription link:</p>
        <div class="link" id="sub-link">{{ sub_url }}</div>
        <br>
        <button onclick="copyLink()">Copy Link</button>
    </div>
    
    <div class="card">
        <h3>How to use:</h3>
        <div class="step"><div class="step-num">1</div>Install Clash, v2rayN, or Shadowrocket</div>
        <div class="step"><div class="step-num">2</div>Copy the link above</div>
        <div class="step"><div class="step-num">3</div>Paste into your app as subscription URL</div>
    </div>
    
    <script>
        function copyLink() {
            navigator.clipboard.writeText(document.getElementById('sub-link').innerText);
            alert('Copied!');
        }
    </script>
</body>
</html>
"""


@app.route('/settings', methods=['GET', 'POST'])
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
            
            <button type="submit">Save Settings</button>
        </form>
    </body>
    </html>
    """, settings=settings)


@app.route('/api/settings', methods=['GET', 'PUT'])
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
        
        db.session.commit()
        return jsonify({'success': True, 'settings': settings.to_dict()})
    
    return jsonify({'settings': settings.to_dict()})


@app.route('/api/sync/all', methods=['POST'])
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
