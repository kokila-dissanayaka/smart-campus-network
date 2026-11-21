# Smart Campus App

Automation and control layer for the Smart Campus Network project (COMP1682).

One codebase, one Docker image, two services:

| Container | Role | Command |
|---|---|---|
| `campus-worker` | Fault detection (10 s ping, DOWN after 2 failures), ISP failover detection via pfSense XML-RPC, scheduled config backups, Telegram/email alerts | `python worker.py` |
| `campus-web` | Flask dashboard: login, device status, ISP links, failover history, ping / backup / interface enable-disable, audit log | `gunicorn web:app` |
| `campus-db` | MySQL 8.0: devices, events, isp_status, failover_events, backups, users, audit_log | — |

## Run
```bash
cp .env.example .env && nano .env
docker compose up -d --build
docker compose exec campus-worker python selftest.py
```
Dashboard: SSH tunnel `-L 5000:127.0.0.1:5000`, then http://localhost:5000

## Layout
```
app/common/config.py   inventory + settings from .env
app/common/db.py       MySQL schema and helpers
app/common/notify.py   Telegram + email
app/common/netops.py   ping, VyOS SSH (Netmiko): backup, interface enable/disable
app/common/pfsense.py  pfSense gateway status + config.xml via XML-RPC
app/common/backup.py   backup logic shared by worker and web
app/worker.py          background service
app/web.py             Flask app
app/selftest.py        connectivity self-test
```
