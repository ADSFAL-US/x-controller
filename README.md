# 3x-controller

Terraform-like subscription management for 3x-ui panels. SQLite is the source of truth.

## Concept

- **SQLite** = Source of truth for all subscriptions
- **3x-ui panels** = Target state to sync to
- **Sync service** = Rate-limited background synchronization (min 5s between syncs, max 10 defers)
- **Web UI + REST API** = Management interface

## Architecture

```
User -> Web UI / REST API -> SQLite (source of truth)
                                      |
                                      v
                              Sync Service (rate-limited)
                                      |
                    +-----------------+-----------------+
                    |                 |                 |
                    v                 v                 v
                Panel 1           Panel 2           Panel N
```

## Fields (3x-ui compatible)

| Field | Description | Default |
|-------|-------------|---------|
| `email` | User email (unique) | required |
| `uuid` | Client ID | auto-generated |
| `total_gb` | Traffic limit (0=unlimited) | 0 |
| `expiry_days` | Days until expiry (0=never) | 0 |
| `enabled` | Account active | true |
| `flow` | VLESS flow type | xtls-rprx-vision |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health + panels status |
| GET | `/api/subscriptions` | List all |
| POST | `/api/subscriptions` | Create |
| GET | `/api/subscriptions/<id>` | Get one |
| PUT | `/api/subscriptions/<id>` | Update |
| DELETE | `/api/subscriptions/<id>` | Delete |
| GET | `/api/panels` | List panels |

## Web UI

- `/` - Dashboard with stats
- `/subscriptions` - List subscriptions
- `/subscriptions/new` - Create form
- `/subscriptions/<id>/edit` - Edit form

## Quick Start

```bash
# 1. Configure panels
edit config/panels.yaml

# 2. Run
sudo bash install.sh

# 3. Open http://localhost:8080
```

## Sync Behavior

1. User creates/updates/deletes subscription via UI/API
2. Subscription saved to SQLite with `sync_status=pending`
3. SyncService schedules sync with rate limiting:
   - Min 5 seconds between syncs
   - If requests come during wait → defer and wait more
   - After 10 defers → force sync anyway
4. SyncService pushes to all panels
5. If panel fails → retry later + mark `sync_status=failed`

## Database = Source of Truth

- Manual changes in 3x-ui panels are **overwritten** by DB state
- Consistency check repairs drift automatically
- Orphan clients (in panel but not in DB) are deleted
