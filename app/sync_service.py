"""Synchronization service - keeps panels in sync with database (source of truth)."""

import json
import logging
import random
import string
from datetime import datetime
from typing import Dict, Optional
import threading
from threading import Thread
import time

from flask import current_app

from app.models import db, Subscription, SyncLog
from app.xui_client import XUIClient

logger = logging.getLogger(__name__)


def _generate_random_email(length: int = 12) -> str:
    """Generate a completely random email string that xui accepts."""
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choices(chars, k=length))


def _get_used_short_ids(inbound: dict) -> set:
    """Extract shortIds already assigned to clients in this inbound."""
    used = set()
    settings_str = inbound.get('settings', '{}')
    try:
        settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str or {}
        clients = settings.get('clients', [])
        for client in clients:
            client_short_ids = client.get('shortIds', [])
            if isinstance(client_short_ids, str):
                client_short_ids = [client_short_ids]
            for sid in client_short_ids:
                if sid:
                    used.add(sid)
    except Exception:
        pass
    return used


def _get_available_short_id(inbound: dict) -> Optional[str]:
    """Get first available shortId from pool that's not used by any client."""
    stream_settings_str = inbound.get('streamSettings', '{}')
    try:
        stream_settings = json.loads(stream_settings_str) if isinstance(stream_settings_str, str) else stream_settings_str or {}
        reality_settings = stream_settings.get('realitySettings', {})
        pool = reality_settings.get('shortIds', reality_settings.get('shortId', []))
        if isinstance(pool, str):
            pool = [pool]
        if not pool:
            return None
        used = _get_used_short_ids(inbound)
        for sid in pool:
            if sid and sid not in used:
                return sid
    except Exception:
        pass
    return None


class SyncService:
    """Service to synchronize subscriptions across all panels."""
    
    def __init__(self, xui_client: XUIClient, min_sync_interval: int = 5, auto_sync_interval: int = 300):
        self.xui_client = xui_client
        self._running = False
        self._thread = None
        self._auto_sync_thread = None
        
        # Rate limiting settings
        self.min_sync_interval = min_sync_interval  # seconds between syncs
        self.auto_sync_interval = auto_sync_interval  # seconds between auto-sync checks (5 min default)
        self._last_sync_time = 0
        self._pending_syncs = {}  # subscription_id -> (subscription, action, defer_count)
        self._max_defers = 10  # max times to defer before forcing sync
        
        # Thread safety: proper lock instead of bool flag
        self._lock = threading.Lock()
        self._sync_in_progress = False
        self._timer = None  # Single timer for delayed sync
        
        # Drift detection state
        self._last_auto_sync = 0
    
    def schedule_sync(self, subscription: Subscription, action: str = 'update'):
        """
        Schedule a sync with rate limiting.
        
        Logic:
        - Wait min_sync_interval between syncs
        - If new requests come during wait, remember them and wait another interval
        - After max_defers (10), force sync anyway
        """
        import time
        
        sub_id = subscription.id
        
        with self._lock:
            # Store/update the pending sync
            if sub_id in self._pending_syncs:
                _, _, defer_count = self._pending_syncs[sub_id]
                self._pending_syncs[sub_id] = (subscription, action, defer_count + 1)
                logger.debug(f"Updated pending sync for {subscription.email} (defer #{defer_count + 1})")
            else:
                self._pending_syncs[sub_id] = (subscription, action, 0)
                logger.debug(f"Scheduled sync for {subscription.email}")
            
            # Check if we should sync now
            current_time = time.time()
            time_since_last = current_time - self._last_sync_time
            
            # Get defer count
            _, _, defer_count = self._pending_syncs[sub_id]
            
            # Sync if: enough time passed OR max defers reached
            if time_since_last >= self.min_sync_interval or defer_count >= self._max_defers:
                if self._sync_in_progress:
                    logger.warning("Sync already in progress, deferring...")
                    return
                
                # Cancel any existing timer since we're syncing now
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                
                self._sync_in_progress = True
                
                # Execute sync in a separate thread to not block
                sync_thread = Thread(target=self._execute_pending_syncs_safe, daemon=True)
                sync_thread.start()
            else:
                # Need to wait, schedule delayed execution
                wait_time = self.min_sync_interval - time_since_last
                logger.info(f"Rate limiting: waiting {wait_time:.1f}s before sync (defer #{defer_count})")
                
                # Cancel existing timer and create new one
                if self._timer:
                    self._timer.cancel()
                
                self._timer = threading.Timer(wait_time, self._execute_pending_syncs_safe)
                self._timer.daemon = True
                self._timer.start()
    
    def _execute_pending_syncs_safe(self):
        """Thread-safe wrapper for executing pending syncs."""
        # Early check: if sync already in progress, skip this execution
        with self._lock:
            if self._sync_in_progress:
                logger.debug("Sync already in progress, skipping this execution")
                return
            self._sync_in_progress = True
            # Clear timer reference since we're executing now
            self._timer = None
        
        try:
            # Need Flask app context for DB operations in background thread
            with current_app.app_context():
                self._execute_pending_syncs()
        finally:
            with self._lock:
                self._sync_in_progress = False
    
    def _execute_pending_syncs(self):
        """Execute all pending syncs."""
        import time
        
        # Copy and clear pending syncs under lock
        with self._lock:
            self._last_sync_time = time.time()
            pending = dict(self._pending_syncs)
            self._pending_syncs.clear()
        
        if not pending:
            return
        
        logger.info(f"Executing {len(pending)} pending syncs")
        
        # Group by action and sort: create (1), delete (2), update (3)
        by_action = {'create': [], 'delete': [], 'update': []}
        for sub_id, (sub, action, _) in pending.items():
            by_action[action].append((sub_id, sub))
        
        # Execute syncs in order: CREATE -> DELETE -> UPDATE
        action_order = ['create', 'delete', 'update']
        for action in action_order:
            items = by_action[action]
            if items:
                logger.info(f"Executing {len(items)} {action} operations")
            for sub_id, sub in items:
                try:
                    # Refresh subscription from DB (might have changed)
                    fresh_sub = Subscription.query.get(sub_id)
                    if fresh_sub:
                        self.sync_subscription(fresh_sub, action)
                except Exception:
                    logger.exception(f"Error syncing {sub.email}")
    
    def sync_subscription(self, subscription: Subscription, action: str = 'create') -> Dict:
        """
        Sync a single subscription to all panels.
        
        Args:
            subscription: Subscription to sync
            action: 'create', 'update', or 'delete'
        
        Returns:
            Dict with results for each panel
        """
        results = {}
        all_success = True
        
        # Track if we generated ss_password and need to save it
        ss_password_generated = False
        
        for panel in self.xui_client.panels:
            # Try to login first
            if not panel.login():
                logger.error(f"Cannot connect to panel {panel.config.name}")
                self._log_sync(subscription, panel.config.name, action, False, "Cannot connect")
                results[panel.config.name] = {'success': False, 'error': 'Cannot connect'}
                all_success = False
                continue
            
            # Get inbounds
            inbounds = panel.get_inbounds()
            if not inbounds:
                logger.warning(f"No inbounds on panel {panel.config.name}")
                self._log_sync(subscription, panel.config.name, action, False, "No inbounds")
                results[panel.config.name] = {'success': False, 'error': 'No inbounds'}
                all_success = False
                continue
            
            panel_success = True
            panel_errors = []
            
            for inbound in inbounds:
                inbound_id = inbound.get('id')
                if not inbound_id:
                    continue
                
                # Determine protocol type from inbound
                protocol = inbound.get('protocol', 'vless').lower()
                
                # Generate client data appropriate for this protocol
                client_data = subscription.to_xui_client(protocol=protocol)
                
                # Track if ss_password was generated
                if protocol == 'shadowsocks' and subscription.ss_password and not ss_password_generated:
                    ss_password_generated = True
                
                # Generate unique random email for this inbound to avoid "Duplicate email"
                client_data['email'] = _generate_random_email()
                
                # Check if inbound uses Reality and determine flow/shortId (only for VLESS)
                if protocol == 'vless':
                    stream_settings_str = inbound.get('streamSettings', '{}')
                    is_reality = False
                    network = 'tcp'
                    try:
                        stream_settings = json.loads(stream_settings_str) if isinstance(stream_settings_str, str) else stream_settings_str or {}
                        is_reality = bool(stream_settings.get('realitySettings'))
                        network = stream_settings.get('network', 'tcp')
                    except Exception:
                        pass
                    
                    # Auto-determine flow based on transport type
                    if is_reality:
                        if network in ('xhttp', 'splithttp', 'httpupgrade'):
                            client_data['flow'] = 'xtls-rprx-vision-udp443'
                        elif network == 'tcp':
                            client_data['flow'] = 'xtls-rprx-vision'
                    
                    # Only assign shortId from pool for NEW clients (create action)
                    # For updates, we will preserve existing shortId from the panel
                    if is_reality and action == 'create':
                        # Get available shortId from panel's pool
                        short_id = _get_available_short_id(inbound)
                        if short_id:
                            client_data['shortIds'] = [short_id]
                            logger.debug(f"Assigned shortId {short_id} for {subscription.email} on inbound {inbound_id}")
                        else:
                            logger.warning(f"No available shortId in pool for {subscription.email} on inbound {inbound_id}")
                
                success = False
                error_msg = None
                
                try:
                    if action == 'create':
                        success = panel.add_client(inbound_id, client_data)
                        if not success:
                            error_msg = f"Failed to add client to inbound {inbound_id}"
                            
                    elif action == 'update':
                        # Find client by appropriate field based on protocol
                        found = None
                        if protocol == 'shadowsocks':
                            # For Shadowsocks, find by password
                            found = self._find_client_by_password(panel, subscription.ss_password, [inbound])
                        else:
                            # For other protocols, find by UUID
                            found = panel.find_client_by_id(subscription.uuid, [inbound])
                        
                        if found:
                            # Use existing client_id from panel
                            client_id = found['client_id']
                            client_data['id'] = client_id
                            # Preserve existing shortId if any (only for VLESS)
                            if protocol == 'vless':
                                existing_short_ids = found['client'].get('shortIds', [])
                                if existing_short_ids:
                                    if isinstance(existing_short_ids, str):
                                        existing_short_ids = [existing_short_ids]
                                    client_data['shortIds'] = existing_short_ids
                                    logger.debug(f"Preserved shortIds {existing_short_ids} for {subscription.email}")
                            success = panel.update_client(inbound_id, client_id, client_data)
                            if not success:
                                error_msg = f"Failed to update client in inbound {inbound_id}"
                        else:
                            # Client not in this inbound, add it (treat as create)
                            success = panel.add_client(inbound_id, client_data)
                            if not success:
                                error_msg = f"Failed to add client to inbound {inbound_id}"
                            
                    elif action == 'delete':
                        # Find client by appropriate field based on protocol
                        found = None
                        if protocol == 'shadowsocks':
                            found = self._find_client_by_password(panel, subscription.ss_password, [inbound])
                        else:
                            found = panel.find_client_by_id(subscription.uuid, [inbound])
                        
                        if found:
                            success = panel.delete_client(inbound_id, found['client_id'])
                            if not success:
                                error_msg = f"Failed to delete client from inbound {inbound_id}"
                        else:
                            success = True  # Already not in this inbound
                    
                except Exception as e:
                    success = False
                    error_msg = str(e)
                    logger.exception(f"Error syncing to panel {panel.config.name}, inbound {inbound_id}")
                
                if not success:
                    panel_success = False
                    if error_msg:
                        panel_errors.append(error_msg)
                    else:
                        panel_errors.append(f"Unknown error in inbound {inbound_id}")
                
                # Log per-inbound attempt
                self._log_sync(subscription, f"{panel.config.name}/inbound-{inbound_id}", action, success, error_msg)
            
            # Save ss_password if it was generated
            if ss_password_generated:
                db.session.commit()
                ss_password_generated = False  # Reset to avoid duplicate saves
            
            # Don't save panel-specific subId - each panel generates its own
            # subId lookup is done dynamically in /sub/<token> route
            
            results[panel.config.name] = {
                'success': panel_success, 
                'error': '; '.join(panel_errors) if panel_errors else None
            }
            
            if not panel_success:
                all_success = False
        
        # Update subscription sync status
        if all_success:
            subscription.sync_status = 'synced'
            subscription.sync_error = None
        else:
            subscription.sync_status = 'failed'
            failed_panels = [k for k, v in results.items() if not v['success']]
            subscription.sync_error = f"Failed on: {', '.join(failed_panels)}"
        
        subscription.last_sync_at = datetime.utcnow()
        db.session.commit()
        
        return results
    
    def _log_sync(self, subscription: Subscription, panel_name: str, 
                  action: str, success: bool, error: Optional[str]):
        """Log a sync attempt."""
        log = SyncLog(
            subscription_id=subscription.id,
            panel_name=panel_name,
            action=action,
            status='success' if success else 'failed',
            error_message=error
        )
        db.session.add(log)
        # Don't commit here, let outer transaction handle it
    
    def sync_all_pending(self):
        """Sync all subscriptions with pending or failed status."""
        pending = Subscription.query.filter(
            Subscription.sync_status.in_(['pending', 'failed'])
        ).all()
        
        logger.info(f"Found {len(pending)} pending/failed subscriptions to sync")
        
        for sub in pending:
            action = 'create' if sub.sync_status == 'pending' else 'update'
            logger.info(f"Syncing subscription {sub.email} ({action})")
            self.sync_subscription(sub, action)
            time.sleep(0.1)  # Small delay between requests
    
    def check_and_repair(self):
        """
        Check panels for consistency and repair if needed.
        This is like 'terraform plan/apply' - finds drift and fixes it.
        """
        logger.info("Starting consistency check...")
        
        # Get all active subscriptions from DB
        db_subs_by_uuid = {sub.uuid: sub for sub in Subscription.query.filter_by(enabled=True).all() if sub.uuid}
        db_subs_by_password = {sub.ss_password: sub for sub in Subscription.query.filter_by(enabled=True).all() if sub.ss_password}
        
        for panel in self.xui_client.panels:
            if not panel.login():
                logger.error(f"Cannot check panel {panel.config.name}")
                continue
            
            inbounds = panel.get_inbounds()
            for inbound in inbounds:
                inbound_id = inbound.get('id')
                protocol = inbound.get('protocol', 'vless').lower()
                
                # Get clients from panel
                clients = inbound.get('settings', {}).get('clients', [])
                
                for client in clients:
                    # Find matching DB subscription by UUID or password
                    db_sub = None
                    if protocol == 'shadowsocks':
                        db_sub = db_subs_by_password.get(client.get('password'))
                    else:
                        db_sub = db_subs_by_uuid.get(client.get('id'))
                    
                    if not db_sub:
                        # Client exists on panel but not in DB - delete it
                        logger.warning(f"Orphan client found: {client.get('email')} on {panel.config.name}")
                        client_id = client.get('id')
                        panel.delete_client(inbound_id, client_id)
                        continue
                    
                    # Check if client matches DB
                    expected = db_sub.to_xui_client()
                    
                    needs_update = False
                    
                    # Check key fields
                    if client.get('totalGB') != expected['totalGB']:
                        needs_update = True
                    if client.get('expiryTime') != expected['expiryTime']:
                        needs_update = True
                    if client.get('enable') != expected['enable']:
                        needs_update = True
                    
                    if needs_update:
                        logger.info(f"Drift detected for {db_sub.email} on {panel.config.name}, repairing...")
                        panel.update_client(inbound_id, expected['id'], expected)
        
        logger.info("Consistency check completed")
    
    def run_periodic_sync(self):
        """
        Periodic auto-sync: compare DB state with all panels and fix drift.
        Runs every auto_sync_interval seconds (default 5 minutes).
        """
        current_time = time.time()
        if current_time - self._last_auto_sync < self.auto_sync_interval:
            return  # Not time yet
        
        self._last_auto_sync = current_time
        logger.info("Starting periodic auto-sync (drift detection)...")
        
        # Get all subscriptions from DB keyed by UUID (id on panels)
        db_subs = {sub.uuid: sub for sub in Subscription.query.all() if sub.uuid}
        
        # Build sync plan for each panel
        for panel in self.xui_client.panels:
            try:
                if not panel.login():
                    logger.error(f"Cannot login to panel {panel.config.name} for auto-sync")
                    continue
                
                sync_plan = self._calculate_panel_diff(panel, db_subs)
                
                if not any(sync_plan.values()):
                    logger.info(f"Panel {panel.config.name} is in sync with DB")
                    continue
                
                logger.info(f"Panel {panel.config.name} sync plan: "
                          f"{len(sync_plan['create'])} creates, "
                          f"{len(sync_plan['delete'])} deletes, "
                          f"{len(sync_plan['update'])} updates")
                
                # Execute plan: 1. creates, 2. deletes, 3. updates
                self._execute_sync_plan(panel, sync_plan)
                
            except Exception:
                logger.exception(f"Error in auto-sync for panel {panel.config.name}")
        
        logger.info("Periodic auto-sync completed")
    
    def _calculate_panel_diff(self, panel, db_subs: Dict[str, Subscription]) -> Dict:
        """
        Calculate difference between DB and panel state.
        Returns plan: {'create': [...], 'delete': [...], 'update': [...]}
        """
        plan = {'create': [], 'delete': [], 'update': []}
        
        # Get current panel state
        inbounds = panel.get_inbounds()
        if not inbounds:
            logger.warning(f"No inbounds on panel {panel.config.name}")
            return plan
        
        # Build map of clients on panel by UUID (id)
        panel_clients = {}  # uuid -> {inbound_id, client_data}
        for inbound in inbounds:
            inbound_id = inbound.get('id')
            settings_str = inbound.get('settings', '{}')
            try:
                settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                for client in settings.get('clients', []):
                    client_id = client.get('id')
                    if client_id:
                        panel_clients[client_id] = {
                            'inbound_id': inbound_id,
                            'client': client
                        }
            except (json.JSONDecodeError, TypeError):
                continue
        
        # Find what needs to be created (in DB but not on panel)
        for uuid, sub in db_subs.items():
            if uuid not in panel_clients:
                if sub.enabled:  # Only sync enabled subscriptions
                    plan['create'].append({
                        'subscription': sub,
                        'reason': 'Not found on panel'
                    })
            else:
                # Check if needs update
                panel_client = panel_clients[uuid]['client']
                expected = sub.to_xui_client()
                
                needs_update = False
                differences = []
                
                if panel_client.get('totalGB') != expected['totalGB']:
                    needs_update = True
                    differences.append(f"totalGB: {panel_client.get('totalGB')} != {expected['totalGB']}")
                
                if panel_client.get('expiryTime') != expected['expiryTime']:
                    needs_update = True
                    differences.append("expiryTime differs")
                
                if panel_client.get('enable') != expected['enable']:
                    needs_update = True
                    differences.append(f"enable: {panel_client.get('enable')} != {expected['enable']}")
                
                if needs_update:
                    plan['update'].append({
                        'subscription': sub,
                        'panel_client': panel_client,
                        'differences': differences,
                        'reason': f"Drift detected: {', '.join(differences)}"
                    })
        
        # Find what needs to be deleted (on panel but not in DB or disabled)
        for client_id, panel_data in panel_clients.items():
            if client_id not in db_subs:
                plan['delete'].append({
                    'client_id': client_id,
                    'inbound_id': panel_data['inbound_id'],
                    'panel_client': panel_data['client'],
                    'reason': 'Orphan client (not in DB)'
                })
            elif not db_subs[client_id].enabled:
                plan['delete'].append({
                    'client_id': client_id,
                    'inbound_id': panel_data['inbound_id'],
                    'panel_client': panel_data['client'],
                    'reason': 'Subscription disabled in DB'
                })
        
        return plan
    
    def _execute_sync_plan(self, panel, plan: Dict):
        """
        Execute sync plan with proper ordering:
        1. Creates (add new clients)
        2. Deletes (remove orphaned/disabled clients)  
        3. Updates (fix drift)
        """
        inbounds = panel.get_inbounds()
        if not inbounds:
            return
        
        # 1. EXECUTE CREATES (highest priority)
        for item in plan['create']:
            try:
                sub = item['subscription']
                
                for inbound in inbounds:
                    inbound_id = inbound.get('id')
                    if not inbound_id:
                        continue
                    
                    # Determine protocol and generate appropriate data
                    protocol = inbound.get('protocol', 'vless').lower()
                    client_data = sub.to_xui_client(protocol=protocol)
                    client_data['email'] = _generate_random_email()
                    
                    # For VLESS with Reality, assign shortId from pool
                    if protocol == 'vless':
                        stream_settings_str = inbound.get('streamSettings', '{}')
                        try:
                            stream_settings = json.loads(stream_settings_str) if isinstance(stream_settings_str, str) else stream_settings_str or {}
                            if stream_settings.get('realitySettings'):
                                short_id = _get_available_short_id(inbound)
                                if short_id:
                                    client_data['shortIds'] = [short_id]
                        except Exception:
                            pass
                    
                    success = panel.add_client(inbound_id, client_data)
                    status = "✓" if success else "✗"
                    logger.info(f"  {status} CREATE {sub.email} in inbound {inbound_id}: {item['reason']}")
                        
            except Exception:
                logger.exception(f"Error creating {sub.email}")
        
        # 2. EXECUTE DELETES (medium priority)
        for item in plan['delete']:
            try:
                success = panel.delete_client(item['inbound_id'], item['client_id'])
                status = "✓" if success else "✗"
                client_email = item.get('panel_client', {}).get('email', 'unknown')
                logger.info(f"  {status} DELETE {client_email}: {item['reason']}")
            except Exception:
                logger.exception(f"Error deleting client {item['client_id']}")
        
        # 3. EXECUTE UPDATES (lowest priority)
        for item in plan['update']:
            try:
                sub = item['subscription']
                
                # Find which inbound has this client
                for inbound in inbounds:
                    inbound_id = inbound.get('id')
                    protocol = inbound.get('protocol', 'vless').lower()
                    settings_str = inbound.get('settings', '{}')
                    try:
                        settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                        for client in settings.get('clients', []):
                            # Match by UUID (VLESS/VMess/Trojan) or password (Shadowsocks)
                            match = False
                            if protocol == 'shadowsocks':
                                match = client.get('password') == sub.ss_password
                            else:
                                match = client.get('id') == sub.uuid
                            
                            if match:
                                client_id = client.get('id')
                                
                                # Generate client data for this protocol
                                client_data = sub.to_xui_client(protocol=protocol)
                                client_data['email'] = client.get('email', _generate_random_email())
                                
                                # IMPORTANT: Preserve existing shortId for VLESS Reality
                                if protocol == 'vless':
                                    existing_short_ids = client.get('shortIds', [])
                                    if existing_short_ids:
                                        if isinstance(existing_short_ids, str):
                                            existing_short_ids = [existing_short_ids]
                                        client_data['shortIds'] = existing_short_ids
                                        logger.debug(f"Force sync: Preserved shortIds {existing_short_ids} for {sub.email}")
                                
                                success = panel.update_client(inbound_id, client_id, client_data)
                                status = "✓" if success else "✗"
                                logger.info(f"  {status} UPDATE {sub.email}: {item['reason']}")
                                break
                    except (json.JSONDecodeError, TypeError):
                        continue
                        
            except Exception:
                logger.exception(f"Error updating {sub.email}")
    
    def start_background_sync(self, interval_seconds: int = 60):
        """Start background thread for periodic sync."""
        if self._running:
            return
        
        self._running = True
        self._thread = Thread(target=self._sync_loop, args=(interval_seconds,), daemon=True)
        self._thread.start()
        logger.info(f"Background sync started (interval: {interval_seconds}s)")
    
    def stop_background_sync(self):
        """Stop background sync thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Background sync stopped")
    
    def _sync_loop(self, interval_seconds: int):
        """Background sync loop with auto-sync."""
        while self._running:
            try:
                # Sync pending subscriptions (user-triggered)
                self.sync_all_pending()
                
                # Periodic auto-sync (drift detection) - runs every auto_sync_interval
                self.run_periodic_sync()
                    
            except Exception:
                logger.exception("Error in sync loop")
            
            # Sleep with break check
            for _ in range(interval_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _find_client_by_password(self, panel, password: str, inbounds: list) -> Optional[dict]:
        """Find client by Shadowsocks password in list of inbounds.
        
        Args:
            panel: XUIPanel instance
            password: Shadowsocks password to search for
            inbounds: List of inbound dicts to search in
            
        Returns:
            Dict with inbound_id, client_id, client_data or None if not found
        """
        if not password:
            return None
            
        for inbound in inbounds:
            inbound_id = inbound.get('id')
            settings_str = inbound.get('settings', '{}')
            try:
                settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
                for client in settings.get('clients', []):
                    if client.get('password') == password:
                        return {
                            'inbound_id': inbound_id,
                            'client_id': client.get('id'),
                            'client': client
                        }
            except (json.JSONDecodeError, TypeError):
                continue
        return None
