"""campus-web: Flask control dashboard (login, status, ISP links, actions)."""
import os
import re
import secrets
import time
from functools import wraps

import pymysql
from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from common import backup, config, db, netops

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=8 * 3600)


def init():
    db.wait_and_init()
    if config.ADMIN_PASSWORD and not db.query("SELECT id FROM users LIMIT 1"):
        try:
            db.execute("INSERT INTO users (username, password_hash, created_at) VALUES (%s,%s,%s)",
                       (config.ADMIN_USER, generate_password_hash(config.ADMIN_PASSWORD), db.now()))
            print(f"[web] admin user '{config.ADMIN_USER}' created", flush=True)
        except pymysql.err.IntegrityError:
            pass  # another gunicorn worker created it first


init()


# ------------------------------------------------------------------ security helpers
def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


@app.before_request
def csrf_protect():
    session.setdefault("csrf", secrets.token_hex(16))
    if request.method == "POST" and request.form.get("csrf") != session["csrf"]:
        abort(400, "Invalid CSRF token")


@app.context_processor
def inject():
    return {"csrf": session.get("csrf", ""), "user": session.get("user"),
            "zabbix_url": config.ZABBIX_URL, "grafana_url": config.GRAFANA_URL}


# ------------------------------------------------------------------ auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        row = db.query("SELECT * FROM users WHERE username=%s", (username,), one=True)
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user"] = username
            session["csrf"] = secrets.token_hex(16)
            session.permanent = True
            db.audit(username, "LOGIN", "web", "success")
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") else url_for("dashboard"))
        db.audit(username or "?", "LOGIN", "web", "failed")
        time.sleep(1)  # slow down password guessing
        flash("Invalid username or password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    if "user" in session:
        db.audit(session["user"], "LOGOUT", "web", "ok")
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ dashboard
def vrrp_view():
    """Which core is MASTER for each VRRP group, plus an overall 'active core'."""
    rows = db.query("SELECT * FROM vrrp_status ORDER BY group_name, core")
    groups = {}
    for r in rows:
        g = groups.setdefault(r["group_name"], {"name": r["group_name"], "master": None,
                                                "iface": r["iface"], "updated_at": r["updated_at"]})
        if r["state"] == "MASTER":
            g["master"], g["priority"] = r["core"], r["priority"]
    groups = sorted(groups.values(), key=lambda g: (any(c.isdigit() for c in g["name"]), g["name"]))
    by_vlan, uplink = {}, None
    for g in groups:
        digits = "".join(c for c in g["name"] if c.isdigit())
        if digits:
            by_vlan[digits] = g["master"]
        elif uplink is None:
            uplink = g["master"]
    masters = {g["master"] for g in groups if g["master"]}
    active = masters.pop() if len(masters) == 1 else ("SPLIT" if masters else None)
    return {"vrrp": groups, "vrrp_vlan": by_vlan, "vrrp_uplink": uplink, "active_core": active}


def load_dashboard(events=15, backups=8, audit=8, failovers=8):
    devices = db.query("SELECT * FROM devices ORDER BY FIELD(dtype,'vyos','pfsense','isp','host'), name")
    return {
        "devices": devices,
        "dev": {d["name"]: d for d in devices},
        "up": sum(1 for d in devices if d["status"] == "UP"),
        "down": sum(1 for d in devices if d["status"] == "DOWN"),
        "isp": db.query("SELECT * FROM isp_status WHERE id=1", one=True),
        **vrrp_view(),
        "failovers": db.query("SELECT * FROM failover_events ORDER BY id DESC LIMIT %s", (failovers,)),
        "events": db.query("SELECT * FROM events ORDER BY id DESC LIMIT %s", (events,)),
        "backups": db.query("SELECT * FROM backups ORDER BY id DESC LIMIT %s", (backups,)),
        "audit": db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (audit,)),
        "interfaces": config.MANAGED_INTERFACES,
        "backup_targets": backup.BACKUP_TARGETS,
        "switches": config.SWITCHES,
    }


# Pages in the navigation bar: endpoint -> (template, data limits)
PAGES = {
    "dashboard": ("overview.html", {"events": 6}),
    "devices": ("devices.html", {"events": 1}),
    "internet": ("internet.html", {"events": 1, "failovers": 30}),
    "events": ("events.html", {"events": 100}),
    "backups": ("backups.html", {"events": 1, "backups": 40}),
    "audit": ("audit.html", {"events": 1, "audit": 60}),
}


def render_page(endpoint):
    template, limits = PAGES[endpoint]
    return render_template(template, page=endpoint, **load_dashboard(**limits))


@app.route("/")
@login_required
def dashboard():
    return render_page("dashboard")


def core_panels():
    """Interface state of each core as campus-worker last stored it (database only: instant)."""
    cores = {}
    for name in config.CORE_ROUTERS:
        raw, vrrp_rows, read_at = db.load_core_state(name)
        age = int((db.now() - read_at).total_seconds()) if read_at else None
        cores[name] = {"ifaces": netops.enrich_interfaces(raw, vrrp_rows), "read_at": read_at,
                       "age": age, "stale": age is None or age > 90}
    return cores


@app.route("/devices")
@login_required
def devices():
    template, limits = PAGES["devices"]
    return render_template(template, page="devices", cores=core_panels(),
                           dev_cfg={n: config.device(n) for n in config.CORE_ROUTERS},
                           if_result=session.pop("if_result", None), **load_dashboard(**limits))


@app.route("/internet")
@login_required
def internet():
    return render_page("internet")


@app.route("/events")
@login_required
def events():
    return render_page("events")


@app.route("/backups")
@login_required
def backups():
    return render_page("backups")


@app.route("/audit")
@login_required
def audit():
    return render_page("audit")


@app.route("/api/status")
@login_required
def api_status():
    data = load_dashboard()
    return jsonify({k: data[k] for k in ("devices", "up", "isp", "failovers", "vrrp", "active_core")})


# ------------------------------------------------------------------ actions
def back_target():
    """Page to return to after an action (only local paths, never another site)."""
    back = request.form.get("back", "")
    return back if back.startswith("/") and not back.startswith("//") else url_for("dashboard")


_PING_SUM = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?([\d.]+)% packet loss")
_PING_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)")


def ping_stats(out):
    m = _PING_SUM.search(out)
    if not m:
        return None
    stats = {"sent": int(m.group(1)), "received": int(m.group(2)), "loss": float(m.group(3))}
    r = _PING_RTT.search(out)
    if r:
        stats.update(min=float(r.group(1)), avg=float(r.group(2)), max=float(r.group(3)))
    return stats


@app.route("/action/ping", methods=["POST"])
@login_required
def action_ping():
    dev = config.device(request.form.get("device", ""))
    if not dev:
        abort(404)
    ok, rtt, out = netops.ping(dev["ip"], count=4, timeout=2)
    db.audit(session["user"], "PING", dev["name"], f"{'ok' if ok else 'failed'} avg={rtt}")
    return render_template("output.html", title=f"Ping {dev['name']} ({dev['ip']})",
                           ok=ok, output=out, stats=ping_stats(out), device=dev,
                           retry=url_for("action_ping"), back=back_target(), ran_at=db.now())


@app.route("/action/backup", methods=["POST"])
@login_required
def action_backup():
    name = request.form.get("device", "")
    if name not in backup.BACKUP_TARGETS:
        abort(404)
    try:
        path, size = backup.backup_device(name, f"web:{session['user']}")
        db.audit(session["user"], "BACKUP", name, f"ok {size} bytes")
        flash(f"Backup of {name} saved ({size} bytes)", "ok")
    except Exception as exc:  # noqa: BLE001
        db.audit(session["user"], "BACKUP", name, f"failed: {exc}")
        flash(f"Backup of {name} failed: {exc}", "error")
    return redirect(back_target())


@app.route("/device/<name>/interfaces")
@login_required
def device_interfaces(name):
    """Old link: the interface view is now a dialog on the Devices page."""
    if name not in config.CORE_ROUTERS:
        abort(404)
    return redirect(url_for("devices", open=name))


@app.route("/action/interface", methods=["POST"])
@login_required
def action_interface():
    dev = config.device(request.form.get("device", ""))
    iface = request.form.get("iface", "")
    action = request.form.get("op", "")
    if not dev or dev["dtype"] != "vyos" or iface not in config.MANAGED_INTERFACES \
            or action not in ("enable", "disable"):
        abort(400)
    target = f"{dev['name']} {iface}"
    try:
        out, ifaces, vrrp_rows = netops.set_interface(dev["ip"], iface, enable=(action == "enable"))
        if any(r["name"].startswith("eth") for r in ifaces):  # only store a sane reading
            db.save_core_state(dev["name"], ifaces, vrrp_rows)  # page shows the new state at once
        db.audit(session["user"], f"IF_{action.upper()}", target, "ok")
        db.log_event(dev["name"], "CONFIG", "WARNING",
                     f"Interface {iface} {action}d by {session['user']} via dashboard")
        session["if_result"] = {"core": dev["name"], "ok": True, "out": out[-1000:],  # small cookie
                                "msg": f"{iface} {action}d on {dev['name']} - committed and saved"}
    except Exception as exc:  # noqa: BLE001
        db.audit(session["user"], f"IF_{action.upper()}", target, f"failed: {exc}")
        session["if_result"] = {"core": dev["name"], "ok": False, "out": str(exc)[-1000:],
                                "msg": f"Could not {action} {iface} on {dev['name']}: {str(exc)[:300]}"}
    return redirect(url_for("devices", open=dev["name"]))


@app.route("/backup/<int:backup_id>")
@login_required
def view_backup(backup_id):
    row = db.query("SELECT * FROM backups WHERE id=%s", (backup_id,), one=True)
    if not row or not row["filename"].startswith(config.BACKUP_DIR) or not os.path.exists(row["filename"]):
        abort(404)
    with open(row["filename"], encoding="utf-8") as fh:
        text = fh.read()
    db.audit(session["user"], "VIEW_BACKUP", row["device"], os.path.basename(row["filename"]))
    return render_template("output.html", title=f"Backup {row['device']} · {row['ts']}",
                           ok=True, output=text, back=url_for("backups"))


@app.template_filter("pill")
def pill(status):
    s = (status or "unknown").lower()
    if s in ("up", "online", "ok", "info"):
        return "ok"
    if s in ("down", "force_down", "high", "none"):
        return "bad"
    if s in ("loss", "delay", "highloss", "highdelay", "warning"):
        return "warn"
    return "muted"
