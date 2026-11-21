"""Configuration backup for core routers (SSH) and pfSense (XML-RPC)."""
import os
from datetime import timedelta

from . import config, db, netops, notify, pfsense

BACKUP_TARGETS = ["Core-1", "Core-2", "pfSense"]


def backup_device(name, triggered_by):
    dev = config.device(name)
    if dev is None or name not in BACKUP_TARGETS:
        raise ValueError(f"{name} does not support backup")
    if dev["dtype"] == "vyos":
        path, size = netops.backup_vyos(name, dev["ip"])
    else:
        path, size = netops.save_backup(name, pfsense.config_xml(), "xml")
    db.execute("INSERT INTO backups (ts, device, filename, size_bytes, triggered_by) "
               "VALUES (%s,%s,%s,%s,%s)", (db.now(), name, path, size, triggered_by))
    db.log_event(name, "BACKUP", "INFO", f"Configuration backup saved ({size} bytes) by {triggered_by}")
    prune(name)
    return path, size


def prune(name, keep=None):
    """Retention policy: keep only the newest `keep` backups of a device (file + row)."""
    keep = config.BACKUP_KEEP if keep is None else keep
    old = db.query("SELECT id, filename FROM backups WHERE device=%s ORDER BY id DESC "
                   "LIMIT 100000 OFFSET %s", (name, keep))
    for row in old:
        fn = row["filename"] or ""
        if fn.startswith(config.BACKUP_DIR) and os.path.exists(fn):
            os.remove(fn)
        db.execute("DELETE FROM backups WHERE id=%s", (row["id"],))
    if old:
        print(f"[backup] {name}: removed {len(old)} old backup(s), keeping {keep}", flush=True)


def stale_targets(max_age_hours=None):
    """Devices with no backup (manual or scheduled) in the last `max_age_hours`."""
    hours = config.BACKUP_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    cutoff = db.now() - timedelta(hours=hours)   # same clock as the stored timestamps
    fresh = {r["device"] for r in db.query(
        "SELECT DISTINCT device FROM backups WHERE ts >= %s", (cutoff,))}
    return [n for n in BACKUP_TARGETS if n not in fresh]


def backup_all(triggered_by, names=None):
    results = {}
    for name in (names or BACKUP_TARGETS):
        try:
            path, size = backup_device(name, triggered_by)
            results[name] = f"ok ({size} bytes)"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"failed: {exc}"
            msg = f"Scheduled configuration backup of {name} failed: {str(exc)[:300]}"
            db.log_event(name, "BACKUP", "WARNING", msg)
            notify.alert(f"⚠️ BACKUP FAILED: {name}", msg)
    return results
