"""Quick connectivity self-test. Run:  docker compose exec campus-worker python selftest.py"""
from common import config, db, netops, notify, pfsense


def step(label, fn):
    try:
        print(f"[ OK ] {label}: {fn()}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {label}: {exc}")


step("MySQL", lambda: db.query("SELECT COUNT(*) AS n FROM devices", one=True))
for d in config.DEVICES:
    step(f"ping {d['name']} {d['ip']}", lambda d=d: netops.ping(d["ip"])[:2])
step("SSH Core-1", lambda: netops.vyos_show("10.0.0.2", "show version | match Version").strip())
step("SSH Core-2", lambda: netops.vyos_show("10.0.0.3", "show version | match Version").strip())
step("pfSense gateways", lambda: pfsense.gateway_status())
step("Telegram", lambda: notify.telegram("🧪 Smart Campus self-test message"))
step("Email", lambda: notify.email("[Smart Campus] self-test", "Self-test email from campus-worker."))
