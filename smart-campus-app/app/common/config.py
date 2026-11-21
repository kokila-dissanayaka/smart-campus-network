"""Central configuration. All secrets come from environment variables (.env file)."""
import os


def env(name, default=""):
    return os.environ.get(name, default)


# ---- Database (campus-db container) ----
DB = {
    "host": env("DB_HOST", "campus-db"),
    "user": env("DB_USER", "campus"),
    "password": env("DB_PASSWORD"),
    "database": env("DB_NAME", "campus"),
}

# ---- Notifications ----
TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")
SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "465"))
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
ALERT_EMAIL_TO = env("ALERT_EMAIL_TO")

# ---- Device credentials ----
ROUTER_USER = env("ROUTER_USER", "netadmin")
ROUTER_PASS = env("ROUTER_PASS")
PFSENSE_HOST = env("PFSENSE_HOST", "10.0.0.1")
PFSENSE_USER = env("PFSENSE_USER", "admin")
PFSENSE_PASS = env("PFSENSE_PASS")

# ---- Worker timing ----
CHECK_INTERVAL = int(env("CHECK_INTERVAL", "10"))        # seconds between device checks
FAIL_THRESHOLD = int(env("FAIL_THRESHOLD", "2"))         # consecutive failures before DOWN
ISP_INTERVAL = int(env("ISP_INTERVAL", "15"))            # seconds between pfSense gateway checks
VRRP_INTERVAL = int(env("VRRP_INTERVAL", "60"))          # seconds between VRRP/interface reads (SSH logins load the routers)
BACKUP_INTERVAL_HOURS = float(env("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_DIR = env("BACKUP_DIR", "/data/backups")
BACKUP_DAILY_AT = env("BACKUP_DAILY_AT", "02:00")          # local time of the daily backup
BACKUP_MAX_AGE_HOURS = float(env("BACKUP_MAX_AGE_HOURS", "24"))  # on start: back up if older
BACKUP_KEEP = int(env("BACKUP_KEEP", "10"))                 # backups kept per device
BACKUP_RETRY_MIN = float(env("BACKUP_RETRY_MIN", "15"))    # retry a failed catch-up backup after N minutes
SITE_GATEWAY = env("SITE_GATEWAY", "pfSense")  # every other device is reached through it

# ---- Web ----
SECRET_KEY = env("FLASK_SECRET", "change-me")
ADMIN_USER = env("ADMIN_USER", "admin")
ADMIN_PASSWORD = env("ADMIN_PASSWORD")

# ---- Inventory ----
# Core routers are managed via their transit (eth0) addresses so that toggling
# a VLAN interface from the dashboard never cuts off management access.
DEVICES = [
    {"name": "Core-1",     "ip": "10.0.0.2",     "role": "L3 core · VRRP pri 200", "dtype": "vyos"},
    {"name": "Core-2",     "ip": "10.0.0.3",     "role": "L3 core · VRRP pri 100", "dtype": "vyos"},
    {"name": "pfSense",    "ip": "10.0.0.1",     "role": "Edge firewall",         "dtype": "pfsense"},
    {"name": "ISP-1",      "ip": "203.0.113.1",  "role": "Primary ISP",           "dtype": "isp"},
    {"name": "ISP-2",      "ip": "198.51.100.1", "role": "Backup ISP",            "dtype": "isp"},
    {"name": "Admin-PC",   "ip": "10.10.10.10",  "role": "VLAN 10 Admin",         "dtype": "host"},
    {"name": "Student-PC", "ip": "10.10.20.10",  "role": "VLAN 20 Student",       "dtype": "host"},
    {"name": "Server",     "ip": "10.10.30.10",  "role": "VLAN 30 Server",        "dtype": "host"},
    {"name": "Mgmt-PC",    "ip": "10.10.99.10",  "role": "VLAN 99 Management",    "dtype": "host"},
]

# Monitoring UIs opened from the header buttons. They are reached through the SSH tunnel,
# so the browser opens them on localhost (change in .env if you use other ports).
ZABBIX_URL = env("ZABBIX_URL", "http://localhost:8080")
GRAFANA_URL = env("GRAFANA_URL", "http://localhost:3000/d/fg4l7bt9yufi8c/smart-campus-network?orgId=1&from=now-1h&to=now&timezone=browser")  # opens the Smart Campus Network dashboard

# pfSense gateway name -> ISP label
ISP_GATEWAYS = {"WANGW": "ISP-1", "WAN2_GW": "ISP-2"}

# VRRP: which core is the gateway for which network. Group names come from `show vrrp`;
# a group whose name has no digits (e.g. TRANSIT) is the uplink towards pfSense, and a
# group whose digits equal a VLAN id (VLAN10 -> 10) is that VLAN's default gateway.
CORE_ROUTERS = ["Core-1", "Core-2"]

# What each core interface is for (shown on the interface page). VyOS descriptions win if set.
INTERFACE_ROLES = {
    "eth0": "Transit to pfSense (10.0.0.0/29) - uplink + management",
    "eth1": "802.1Q trunk to access switch (VLAN 10, 20)",
    "eth2": "802.1Q trunk to access switch (VLAN 30, 99)",
    "eth3": "Core-to-core link (OSPF area 0, 10.0.1.0/30)",
    "eth1.10": "VLAN 10 Admin - default gateway",
    "eth1.20": "VLAN 20 Student - default gateway",
    "eth2.30": "VLAN 30 Server - default gateway",
    "eth2.99": "VLAN 99 Management - default gateway",
    "lo": "Loopback",
}

# Layer-2 switches (GNS3 built-in Ethernet switches, 8 ports each). They have no IP, SSH or
# SNMP, so they cannot be polled: the dashboard draws them from this port map and infers
# their state from the devices behind them.
SWITCHES = {
    "SW-T": {"role": "Transit switch: pfSense LAN + both core uplinks (10.0.0.0/29)", "size": 8,
             "behind": ["Core-1", "Core-2"],
             "ports": {0: "pfSense em1 (LAN)", 1: "Core-1 eth0", 2: "Core-2 eth0"}},
    "SW-1": {"role": "Access switch: Admin + Student", "size": 8, "behind": ["Admin-PC", "Student-PC"],
             "ports": {0: "Core-1 eth1 - trunk VLAN 10,20", 1: "Core-2 eth1 - trunk VLAN 10,20",
                       2: "Admin-PC - access VLAN 10", 3: "Student-PC - access VLAN 20"}},
    "SW-2": {"role": "Access switch: Server + Management", "size": 8, "behind": ["Server", "Mgmt-PC"],
             "ports": {0: "Core-1 eth2 - trunk VLAN 30,99", 1: "Core-2 eth2 - trunk VLAN 30,99",
                       2: "Server - access VLAN 30", 3: "Mgmt-PC - access VLAN 99"}},
}

# Interfaces that may be enabled/disabled from the dashboard (eth0/eth3 excluded on purpose)
MANAGED_INTERFACES = ["eth1", "eth2", "eth1.10", "eth1.20", "eth2.30", "eth2.99"]


def device(name):
    for d in DEVICES:
        if d["name"] == name:
            return d
    return None
