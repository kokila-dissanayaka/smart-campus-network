"""campus-worker: background automation service.

* Fast fault detection: pings every device every CHECK_INTERVAL seconds and raises a
  DOWN alert after FAIL_THRESHOLD consecutive failures (target: < 60 s).
* ISP failover detection: reads pfSense gateway status over XML-RPC and alerts when the
  active ISP changes, recording failover/failback events with their duration.
* VRRP + interface tracking: reads `show vrrp` and `show interfaces` on both cores (SSH) and alerts when the
  master router of any group changes (e.g. Core-1 -> Core-2).
* Configuration backups of Core-1, Core-2 and pfSense: daily at BACKUP_DAILY_AT, plus a
  start-up check that backs up any device without a backup in the last 24 h; the newest
  BACKUP_KEEP backups per device are kept.
Every state change is written to MySQL and sent to Telegram + email.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from common import backup, config, db, netops, notify, pfsense

state = {d["name"]: {"status": "UNKNOWN", "fails": 0, "down_since": None} for d in config.DEVICES}
isp_state = {"active": None, "api_ok": True}
vrrp_masters = {}  # group -> core name currently MASTER


def fmt_duration(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


# ------------------------------------------------------------------ devices
def check_one(dev):
    ok, rtt, _ = netops.ping(dev["ip"], count=1, timeout=2)
    return dev, ok, rtt


site_state = {"down_since": None}


def check_devices(pool):
    """Ping every device. Dependency-aware: every device is monitored *through* pfSense
    (WireGuard tunnel), so when pfSense itself is down the others are marked UNREACHABLE
    and ONE 'site unreachable' alert is sent instead of an alert storm."""
    ts = db.now()
    results = list(pool.map(check_one, config.DEVICES))
    gw_name = config.SITE_GATEWAY
    results.sort(key=lambda r: r[0]["name"] != gw_name)   # evaluate the gateway first
    site_down = False
    for dev, ok, rtt in results:
        st = state[dev["name"]]
        name = dev["name"]
        changed = False
        if ok:
            if st["status"] == "DOWN":
                downtime = (ts - st["down_since"]).total_seconds()
                msg = f"{name} ({dev['ip']}) is reachable again after {fmt_duration(downtime)}."
                db.log_event(name, "RECOVERY", "INFO", msg)
                if name == gw_name:
                    msg = (f"Campus site is reachable again after {fmt_duration(downtime)} "
                           f"(pfSense / WireGuard path restored).")
                    notify.alert("✅ SITE REACHABLE: campus", msg)
                else:
                    notify.alert(f"✅ RECOVERED: {name}", msg)
            elif st["status"] == "UNREACHABLE":
                db.log_event(name, "RECOVERY", "INFO", f"{name} reachable again (site path restored).")
            changed = st["status"] != "UP"
            st.update(status="UP", fails=0, down_since=None)
        else:
            st["fails"] += 1
            if st["fails"] == 1:
                st["first_fail"] = ts  # outage starts at the first failed check, not at confirmation
            confirmed = st["fails"] >= config.FAIL_THRESHOLD
            if name != gw_name and site_down:
                # Not its own fault: the path to the whole site is down. No individual alert.
                if st["status"] not in ("UNREACHABLE", "DOWN"):
                    st.update(status="UNREACHABLE", down_since=st.get("first_fail") or ts)
                    changed = True
            elif st["status"] not in ("DOWN",) and confirmed:
                st.update(status="DOWN", down_since=st.get("first_fail") or ts)
                changed = True
                if name == gw_name:
                    others = len(config.DEVICES) - 1
                    msg = (f"Campus site unreachable from the cloud monitor: pfSense ({dev['ip']}) "
                           f"failed {st['fails']} consecutive checks. Probable cause: WAN / WireGuard "
                           f"path down. The other {others} devices are marked UNREACHABLE "
                           f"(alerts suppressed).")
                    db.log_event(name, "SITE", "HIGH", msg)
                    notify.alert("🔴 SITE UNREACHABLE: campus", msg)
                else:
                    msg = (f"{name} ({dev['ip']}, {dev['role']}) failed {st['fails']} consecutive "
                           f"checks ({config.CHECK_INTERVAL}s interval).")
                    db.log_event(name, "DOWN", "HIGH", msg)
                    notify.alert(f"🔴 DOWN: {name}", msg)
        if name == gw_name:
            site_down = st["status"] == "DOWN"
        db.execute(
            "UPDATE devices SET status=%s, rtt_ms=%s, last_check=%s"
            + (", last_change=%s" if changed else "") + " WHERE name=%s",
            (st["status"], rtt if ok else None, ts) + ((ts,) if changed else ()) + (name,))


# ------------------------------------------------------------------ ISP / pfSense
def check_isp():
    try:
        gws = pfsense.gateway_status()
    except Exception as exc:  # noqa: BLE001
        if isp_state["api_ok"]:
            isp_state["api_ok"] = False
            db.log_event("pfSense", "API", "WARNING", f"Cannot read gateway status: {exc}")
            print(f"[isp] pfSense API error: {exc}", flush=True)
        return
    if not isp_state["api_ok"]:
        isp_state["api_ok"] = True
        db.log_event("pfSense", "API", "INFO", "Gateway status API reachable again")

    g1, g2 = gws.get("WANGW", {}), gws.get("WAN2_GW", {})
    active = pfsense.active_isp(gws)
    ts = db.now()
    db.execute(
        "REPLACE INTO isp_status (id, active_isp, isp1_status, isp1_delay, isp1_loss, "
        "isp2_status, isp2_delay, isp2_loss, updated_at) VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s)",
        (active, g1.get("status"), g1.get("delay"), g1.get("loss"),
         g2.get("status"), g2.get("delay"), g2.get("loss"), ts))

    previous = isp_state["active"]
    isp_state["active"] = active
    if previous is None:
        # First reading after a worker start: if the primary is already in use, any outage
        # record still open (e.g. the worker was restarted mid-outage) is finished now.
        if active == "ISP-1":
            close_open_outages(ts)
        return
    if previous == active:
        return

    reason = f"ISP-1 gateway: {g1.get('status')} (loss {g1.get('loss')}); ISP-2 gateway: {g2.get('status')}"
    if active == "ISP-1":
        secs = close_open_outages(ts)
        duration = f" Outage on primary ISP lasted {fmt_duration(secs)}." if secs is not None else ""
        msg = f"Traffic restored to primary ISP-1 (from {previous}).{duration}"
        db.log_event("pfSense", "FAILBACK", "INFO", msg)
        notify.alert("✅ ISP FAILBACK: ISP-1 active", msg)
    else:
        # One outage = one record, from leaving ISP-1 until returning to it. Moves between
        # ISP-2 and NONE inside the same outage update that record instead of opening another.
        row = db.query("SELECT id FROM failover_events WHERE ended_at IS NULL "
                       "ORDER BY id LIMIT 1", one=True)
        if row:
            db.execute("UPDATE failover_events SET to_isp=%s, reason=%s WHERE id=%s",
                       (active, reason[:250], row["id"]))
        else:
            db.execute("INSERT INTO failover_events (started_at, from_isp, to_isp, reason) "
                       "VALUES (%s,%s,%s,%s)", (ts, previous, active, reason[:250]))
        msg = f"Active internet link changed {previous} -> {active}. {reason}"
        severity = "HIGH" if active == "NONE" else "WARNING"
        db.log_event("pfSense", "FAILOVER", severity, msg)
        title = "🔴 ALL ISPs DOWN" if active == "NONE" else f"⚠️ ISP FAILOVER: {previous} → {active}"
        notify.alert(title, msg)


def close_open_outages(ts):
    """Close every open failover record; returns the length of the oldest one (seconds)."""
    rows = db.query("SELECT id, started_at FROM failover_events WHERE ended_at IS NULL ORDER BY id")
    for r in rows:
        db.execute("UPDATE failover_events SET ended_at=%s, duration_s=%s WHERE id=%s",
                   (ts, int((ts - r["started_at"]).total_seconds()), r["id"]))
    return int((ts - rows[0]["started_at"]).total_seconds()) if rows else None


# ------------------------------------------------------------------ VRRP (which core is active)
def read_core(name):
    try:
        ifaces, rows = netops.read_core(config.device(name)["ip"])
        return name, ifaces, rows, None
    except Exception as exc:  # noqa: BLE001 - core down / SSH refused
        return name, [], [], exc


def check_vrrp(pool):
    """Read interfaces + VRRP of both cores (one SSH session each) and store them, so the
    dashboard pages read the database instantly instead of waiting for SSH."""
    ts = db.now()
    masters = {}
    for name, ifaces, rows, err in pool.map(read_core, config.CORE_ROUTERS):
        if err is not None:
            # Router unreachable: whatever it reported before is no longer true.
            db.execute("UPDATE vrrp_status SET state='UNKNOWN', updated_at=%s WHERE core=%s", (ts, name))
            continue
        if any(r["name"].startswith("eth") for r in ifaces):  # ignore a garbled reading
            db.save_core_state(name, ifaces, rows)
        for r in rows:
            if r["state"] == "MASTER":
                masters[r["group"]] = name
    for group, core in masters.items():
        before = vrrp_masters.get(group)
        if before and before != core:
            msg = f"VRRP group {group}: gateway moved {before} -> {core}."
            db.log_event(core, "VRRP", "WARNING", msg)
            notify.alert(f"🔁 VRRP: {group} now on {core}", msg)
    vrrp_masters.update(masters)


def vrrp_loop():
    """Runs in its own thread so slow SSH logins never delay the 10 s ping checks."""
    with ThreadPoolExecutor(max_workers=len(config.CORE_ROUTERS)) as pool:
        while True:
            started = time.time()
            try:
                check_vrrp(pool)
            except Exception as exc:  # noqa: BLE001
                print(f"[vrrp] error: {exc}", flush=True)
            time.sleep(max(2.0, config.VRRP_INTERVAL - (time.time() - started)))


# ------------------------------------------------------------------ main loop
def main():
    db.wait_and_init()
    db.log_event("worker", "START", "INFO", "campus-worker started")
    notify.alert("🟢 campus-worker started",
                 f"Checking {len(config.DEVICES)} devices every {config.CHECK_INTERVAL}s.")
    threading.Thread(target=vrrp_loop, name="vrrp", daemon=True).start()
    last_isp = 0.0
    startup_check_at = time.time() + 120          # ~2 min after start: back up stale devices
    hh, mm = (int(x) for x in config.BACKUP_DAILY_AT.split(":"))
    now_local = time.localtime()
    # if we start after today's backup time, the next daily run is tomorrow
    last_daily = time.strftime("%Y-%m-%d") if (now_local.tm_hour, now_local.tm_min) >= (hh, mm) else None
    with ThreadPoolExecutor(max_workers=len(config.DEVICES)) as pool:
        while True:
            started = time.time()
            try:
                check_devices(pool)
                if started - last_isp >= config.ISP_INTERVAL:
                    check_isp()
                    last_isp = started
                if startup_check_at and started >= startup_check_at:
                    # Start-up catch-up: back up devices with no backup in the last 24 h. The
                    # cloud VM often starts before the campus lab, so failed devices are
                    # retried every BACKUP_RETRY_MIN minutes until all are backed up.
                    stale = backup.stale_targets()
                    if stale:
                        res = backup.backup_all("scheduler:startup", stale)
                        print(f"[backup] catch-up for {stale}: {res}", flush=True)
                        failed = [n for n, r in res.items() if r.startswith("failed")]
                    else:
                        failed = []
                        print("[backup] catch-up: all devices backed up in the last 24 h - skipped", flush=True)
                    startup_check_at = started + config.BACKUP_RETRY_MIN * 60 if failed else None
                lt = time.localtime()
                today = time.strftime("%Y-%m-%d", lt)
                if (lt.tm_hour, lt.tm_min) >= (hh, mm) and last_daily != today:
                    last_daily = today
                    print(f"[backup] daily {config.BACKUP_DAILY_AT}: {backup.backup_all('scheduler:daily')}", flush=True)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                print(f"[worker] cycle error: {exc}", flush=True)
            time.sleep(max(1.0, config.CHECK_INTERVAL - (time.time() - started)))


if __name__ == "__main__":
    main()
