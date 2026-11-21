"""MySQL helper: schema creation and small query helpers (PyMySQL)."""
import time
from datetime import datetime

import pymysql

from .config import DB, DEVICES

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) UNIQUE NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        created_at DATETIME NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS devices (
        name VARCHAR(50) PRIMARY KEY,
        ip VARCHAR(45) NOT NULL,
        role VARCHAR(100),
        dtype VARCHAR(20),
        status VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN',
        rtt_ms FLOAT NULL,
        last_check DATETIME NULL,
        last_change DATETIME NULL)""",
    """CREATE TABLE IF NOT EXISTS events (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ts DATETIME NOT NULL,
        device VARCHAR(50),
        etype VARCHAR(30),
        severity VARCHAR(10),
        message TEXT,
        INDEX idx_events_ts (ts))""",
    """CREATE TABLE IF NOT EXISTS isp_status (
        id INT PRIMARY KEY,
        active_isp VARCHAR(10),
        isp1_status VARCHAR(20), isp1_delay VARCHAR(20), isp1_loss VARCHAR(20),
        isp2_status VARCHAR(20), isp2_delay VARCHAR(20), isp2_loss VARCHAR(20),
        updated_at DATETIME)""",
    """CREATE TABLE IF NOT EXISTS vrrp_status (
        group_name VARCHAR(30) NOT NULL,
        core VARCHAR(20) NOT NULL,
        iface VARCHAR(30),
        state VARCHAR(10),
        priority INT NULL,
        updated_at DATETIME,
        PRIMARY KEY (group_name, core))""",
    """CREATE TABLE IF NOT EXISTS iface_status (
        core VARCHAR(20) NOT NULL,
        name VARCHAR(40) NOT NULL,
        ips VARCHAR(255),
        admin VARCHAR(8),
        link VARCHAR(8),
        descr VARCHAR(120),
        seq INT,
        updated_at DATETIME,
        PRIMARY KEY (core, name))""",
    """CREATE TABLE IF NOT EXISTS failover_events (
        id INT AUTO_INCREMENT PRIMARY KEY,
        started_at DATETIME NOT NULL,
        ended_at DATETIME NULL,
        from_isp VARCHAR(10),
        to_isp VARCHAR(10),
        reason VARCHAR(255),
        duration_s INT NULL)""",
    """CREATE TABLE IF NOT EXISTS backups (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ts DATETIME NOT NULL,
        device VARCHAR(50),
        filename VARCHAR(255),
        size_bytes INT,
        triggered_by VARCHAR(50))""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ts DATETIME NOT NULL,
        username VARCHAR(50),
        action VARCHAR(50),
        target VARCHAR(100),
        result VARCHAR(255))""",
]


def now():
    return datetime.now().replace(microsecond=0)


def connect():
    return pymysql.connect(
        **DB, autocommit=True, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=5)


def query(sql, args=None, one=False):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=None):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.lastrowid


def wait_and_init(retries=40, delay=5):
    """Wait for MySQL to come up, then create tables and seed the device inventory."""
    for attempt in range(1, retries + 1):
        try:
            for stmt in SCHEMA:
                execute(stmt)
            # older databases: status column was VARCHAR(10), too short for 'UNREACHABLE'
            execute("ALTER TABLE devices MODIFY status VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN'")
            for d in DEVICES:
                execute(
                    "INSERT INTO devices (name, ip, role, dtype) VALUES (%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE ip=VALUES(ip), role=VALUES(role), dtype=VALUES(dtype)",
                    (d["name"], d["ip"], d["role"], d["dtype"]))
            print("[db] schema ready", flush=True)
            return
        except pymysql.err.OperationalError as exc:
            print(f"[db] waiting for MySQL ({attempt}/{retries}): {exc}", flush=True)
            time.sleep(delay)
    raise RuntimeError("MySQL did not become available")


def log_event(device, etype, severity, message):
    execute("INSERT INTO events (ts, device, etype, severity, message) VALUES (%s,%s,%s,%s,%s)",
            (now(), device, etype, severity, message))


def audit(username, action, target, result):
    execute("INSERT INTO audit_log (ts, username, action, target, result) VALUES (%s,%s,%s,%s,%s)",
            (now(), username, action, target, str(result)[:250]))


def save_core_state(core, ifaces, vrrp_rows):
    """Store a fresh `show interfaces` / `show vrrp` reading of one core router."""
    ts = now()
    with connect() as conn:
        conn.begin()  # one transaction: readers never see a half-written table
        with conn.cursor() as cur:
            cur.execute("DELETE FROM iface_status WHERE core=%s", (core,))
            for i, r in enumerate(ifaces):
                cur.execute(
                    "INSERT INTO iface_status (core, name, ips, admin, link, descr, seq, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (core, r["name"], " ".join(r["ips"])[:255], r["admin"], r["link"],
                     (r.get("desc") or "")[:120], i, ts))
            for r in vrrp_rows:
                cur.execute(
                    "REPLACE INTO vrrp_status (group_name, core, iface, state, priority, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (r["group"], core, r["iface"], r["state"], r["priority"], ts))
        conn.commit()


def load_core_state(core):
    """Return (interfaces, vrrp rows, read time) as last stored for one core."""
    rows = query("SELECT * FROM iface_status WHERE core=%s ORDER BY seq", (core,))
    ifaces = [{"name": r["name"], "ips": (r["ips"] or "").split(), "admin": r["admin"],
               "link": r["link"], "desc": r["descr"] or ""} for r in rows]
    vrrp = [{"group": r["group_name"], "iface": r["iface"], "state": r["state"],
             "priority": r["priority"]}
            for r in query("SELECT * FROM vrrp_status WHERE core=%s", (core,))]
    read_at = max((r["updated_at"] for r in rows), default=None)
    return ifaces, vrrp, read_at
