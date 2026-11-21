"""Network operations: ICMP ping and VyOS management over SSH (Netmiko)."""
import os
import re
import time
import subprocess

from netmiko import ConnectHandler

from . import config
from .db import now

_RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


def ping(ip, count=1, timeout=2):
    """Return (reachable, avg_rtt_ms or None, raw_output)."""
    try:
        proc = subprocess.run(
            ["ping", "-n", "-c", str(count), "-W", str(timeout), ip],
            capture_output=True, text=True, timeout=count * timeout + 5)
    except subprocess.TimeoutExpired:
        return False, None, "ping timed out"
    out = proc.stdout + proc.stderr
    rtts = [float(x) for x in _RTT_RE.findall(out)]
    ok = proc.returncode == 0 and bool(rtts)
    return ok, (round(sum(rtts) / len(rtts), 1) if rtts else None), out


# VyOS (bash readline) wraps every prompt in "bracketed paste" escape codes such as
# ESC[?2004h / ESC[?2004l. They break Netmiko's echo/prompt matching (slow commands) and
# end up in saved output, so they are switched off per session and stripped from output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[=>]|\x1b")
_PROMPT = r"@[\w.-]+(?::\S*)?[#$]"   # netadmin@Core-1:~$ (operational) or netadmin@Core-1# (configure)


def clean(text):
    return _ANSI_RE.sub("", text or "").replace("\r", "")


def _vyos(ip, session_log=None):
    conn = ConnectHandler(
        device_type="vyos", host=ip,
        username=config.ROUTER_USER, password=config.ROUTER_PASS,
        conn_timeout=30, auth_timeout=60, banner_timeout=60, fast_cli=False, session_log=session_log)
    try:  # silence detection, no prompt pattern needed
        conn.send_command_timing("bind 'set enable-bracketed-paste off'", cmd_verify=False,
                                 last_read=1.0, read_timeout=10)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    return conn


def vyos_show(ip, command):
    with _vyos(ip) as conn:
        return clean(conn.send_command(command, read_timeout=30))


_VRRP_STATES = {"MASTER", "BACKUP", "FAULT", "INIT"}


def parse_vrrp(text):
    """Parse VyOS `show vrrp` into [{group, iface, state, priority}]."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        hit = [i for i, p in enumerate(parts) if p.upper() in _VRRP_STATES]
        if len(parts) < 3 or not hit:
            continue
        i = hit[0]
        prio = parts[i + 1] if i + 1 < len(parts) and parts[i + 1].isdigit() else None
        rows.append({"group": parts[0], "iface": parts[1], "state": parts[i].upper(),
                     "priority": int(prio) if prio else None})
    return rows


def read_core(ip, conn=None):
    """`show interfaces` + `show vrrp` in ONE SSH session -> (interfaces, vrrp rows)."""
    if conn is None:
        with _vyos(ip) as c:
            return read_core(ip, c)
    ifaces = parse_interfaces(clean(conn.send_command("show interfaces", read_timeout=90)))
    vrrp = parse_vrrp(clean(conn.send_command("show vrrp", read_timeout=90)))
    return ifaces, vrrp


def backup_vyos(name, ip):
    """Pull the running configuration as 'set' commands and save it to disk."""
    text = vyos_show(ip, "show configuration commands")
    return save_backup(name, text, "txt")


def save_backup(name, text, ext):
    folder = os.path.join(config.BACKUP_DIR, name)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}_{now():%Y%m%d_%H%M%S}.{ext}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path, len(text.encode("utf-8"))


_IP_RE = re.compile(r"^[0-9a-fA-F.:]+/\d{1,3}$")
_IFNAME_RE = re.compile(r"^(eth|lo|wg|bond|br|dum|vti|tun|veth|pppoe)[\w.]*$")
_SL_RE = re.compile(r"^([uDA])/([uD])$")
_VRRP_VIF_RE = re.compile(r"v\d+v[46]$")


def parse_interfaces(text):
    """Parse VyOS `show interfaces` (S/L column) into a list of dicts."""
    rows, cur = [], None
    for line in text.splitlines():
        if not line.strip() or line.startswith(("Codes", "---", "Interface")):
            continue
        toks = line.split()
        if not line[0].isspace():
            if not _IFNAME_RE.match(toks[0]):   # prompt or other noise, not an interface
                cur = None
                continue
            cur = {"name": toks[0], "ips": [], "admin": None, "link": None, "desc": ""}
            rows.append(cur)
            for i, t in enumerate(toks[1:], 1):
                m = _SL_RE.match(t)
                if m:
                    cur["admin"] = "up" if m.group(1) == "u" else "down"
                    cur["link"] = "up" if m.group(2) == "u" else "down"
                    cur["desc"] = " ".join(toks[i + 1:])
                    break
                if _IP_RE.match(t):
                    cur["ips"].append(t)
        elif cur is not None:
            cur["ips"] += [t for t in toks if _IP_RE.match(t)]
    return rows


def enrich_interfaces(ifaces, vrrp_rows):
    """Add purpose, usage (in use / not used), state and VRRP role to parsed interfaces."""
    vrrp = {r["iface"]: r for r in vrrp_rows}
    names = {r["name"] for r in ifaces}
    out = []
    for raw in ifaces:
        r = dict(raw)
        n = r["name"]
        r["vrrp_virtual"] = bool(_VRRP_VIF_RE.search(n))
        r["children"] = sorted(x for x in names if x.startswith(n + ".") and not _VRRP_VIF_RE.search(x))
        r["vrrp"] = vrrp.get(n)
        r["managed"] = n in config.MANAGED_INTERFACES
        r["role"] = r.get("desc") or config.INTERFACE_ROLES.get(n, "")
        if r["vrrp_virtual"] and not r["role"]:
            r["role"] = "VRRP virtual-MAC interface (exists only on the master)"
        # In use = carries an address, is a VLAN parent, or runs VRRP. Otherwise it is spare.
        r["used"] = bool(r["ips"] or r["children"] or r["vrrp"] or r["vrrp_virtual"])
        if r["admin"] == "down":
            r["state"] = "DISABLED"
        elif r["link"] == "down":
            r["state"] = "LINK DOWN"
        elif r["admin"] == "up":
            r["state"] = "UP"
        else:
            r["state"] = "UNKNOWN"
        out.append(r)
    return out


def _iface_path(iface):
    if iface not in config.MANAGED_INTERFACES:
        raise ValueError(f"interface {iface} is not allowed")
    if "." in iface:
        base, vif = iface.split(".", 1)
        return f"interfaces ethernet {base} vif {vif}"
    return f"interfaces ethernet {iface}"


def session_log_path(ip):
    return f"/tmp/netmiko-{ip}.log"


def set_interface(ip, iface, enable):
    """Enable/disable an interface, commit + save, then re-read the router.
    Returns (router output, interfaces, vrrp rows).

    Each step waits for the VyOS prompt (netadmin@Core-1:~$ or netadmin@Core-1#, as seen
    in the session log) so commands are never typed ahead, and the result is verified by
    reading the interface state back in a fresh session. Every step is timed, and the raw SSH session is written to
    /tmp/netmiko-<ip>.log inside the container for troubleshooting."""
    path = _iface_path(iface)
    cmd = f"delete {path} disable" if enable else f"set {path} disable"
    # (command, max seconds). Each step waits for the router prompt before the next one
    # is sent; generous limits because a loaded GNS3 router can take tens of seconds.
    steps = [("configure", 90), (cmd, 90), ("commit", 300), ("save", 180), ("exit", 90)]
    out = []
    started = time.time()
    with _vyos(ip, session_log=session_log_path(ip)) as conn:
        out.append(f"(login {time.time() - started:.1f}s)")
        for command, limit in steps:
            t0 = time.time()
            try:
                text = clean(conn.send_command(command, expect_string=_PROMPT, cmd_verify=False,
                                               read_timeout=limit, strip_prompt=False))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"step '{command}' failed after {time.time() - t0:.1f}s: {exc}") from exc
            out.append(f"$ {command}   ({time.time() - t0:.1f}s)\n{text.strip()}")
            if command == "commit" and ("failed" in text.lower() or "error" in text.lower()):
                conn.send_command("exit discard", expect_string=_PROMPT, cmd_verify=False, read_timeout=90)
                raise RuntimeError(f"commit failed on router:\n{text.strip()}")
    time.sleep(2)  # give VRRP a moment to change state before reading it back
    t0 = time.time()
    ifaces, vrrp = read_core(ip)
    out.append(f"(read-back {time.time() - t0:.1f}s, total {time.time() - started:.1f}s)")
    row = next((r for r in ifaces if r["name"] == iface), None)
    want = "up" if enable else "down"
    if row is None or row["admin"] != want:
        raise RuntimeError(f"router did not apply the change: {iface} admin state is "
                           f"{row['admin'] if row else 'missing'}, expected {want}\n" + "\n".join(out))
    return "\n".join(out), ifaces, vrrp
