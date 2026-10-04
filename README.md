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
# 1. Create and configure the local panel settings
cp config/panels.example.yaml config/panels.yaml
edit config/panels.yaml

# 2. Run
sudo bash install.sh

# 3. Open http://localhost:8080
```

## Panel API Modes

Each panel can select its 3x-ui API mode in the local `config/panels.yaml`. This file is intentionally untracked because it contains environment-specific panel addresses and credentials. Start from the tracked `config/panels.example.yaml` template:

```bash
cp config/panels.example.yaml config/panels.yaml
```

- `legacy: true` uses the existing username/password login and inbound client endpoints for 3x-ui 2.9.x. If `legacy` is omitted, it defaults to `true`.
- `legacy: false` uses the separate clients API available from 3x-ui 3.1.0. Configure `api_token` with a token created in **Settings > Security > API Tokens**. Authentication is sent as `Authorization: Bearer <token>`; username/password are not used and may be omitted.

Modern panels keep one client entity per subscription and attach it to the panel's inbounds. The adapter generates a panel email for a new client and preserves it on subsequent syncs. Keep API tokens out of source control and restrict access to the panel configuration file.

`panel_path`, `sub_host`, and `sub_path` continue to control panel and subscription URLs in both modes. See `config/panels.example.yaml` for examples.

Existing installations need a one-time migration because `config/panels.yaml` was previously tracked. The old installer may stop the container and then fail its pull when either it or `panels.yaml` has local edits. Run this once on the server to back up both files, restore their tracked versions, pull the update, and put the production config back:

```bash
cd /opt/3x-controller
backup_dir=$(mktemp -d)
chmod 700 "$backup_dir"
cp -p config/panels.yaml "$backup_dir/panels.yaml"
cp -p install.sh "$backup_dir/install.sh"
git restore --source=HEAD --staged --worktree -- config/panels.yaml install.sh
if git pull --ff-only origin master; then
        cp -p "$backup_dir/panels.yaml" config/panels.yaml
        chmod 600 config/panels.yaml
        echo "Local install.sh backup: $backup_dir/install.sh"
else
        cp -p "$backup_dir/panels.yaml" config/panels.yaml
        cp -p "$backup_dir/install.sh" install.sh
        echo "Update failed; original files were restored. Backup: $backup_dir"
        exit 1
fi
```

Review the backed-up `install.sh` for any intentional server-specific edits. Subsequent updates preserve `panels.yaml`, keep the service running if Git refuses the update, and retain any local installer version in `data/update-backup.*` for comparison.

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
