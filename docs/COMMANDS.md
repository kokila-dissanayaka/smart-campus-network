# Operations Command Reference

Commands used to build, check and troubleshoot the Smart Campus Network.
Replace values in `<angle brackets>` with your own. Never paste real passwords into files
that are committed to Git.

---

## 1. Administrator laptop (Windows PowerShell)

Open the SSH tunnel to the Azure monitoring VM (dashboard, Zabbix, Grafana):

```powershell
ssh -i .\<key>.pem -L 5000:127.0.0.1:5000 -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 azureuser@<vm-public-ip>
```

Then browse to:

| Service | URL |
|---|---|
| Web control dashboard | http://localhost:5000 |
| Zabbix | http://localhost:8080 |
| Grafana | http://localhost:3000 |

Fix "UNPROTECTED PRIVATE KEY FILE" on Windows:

```powershell
icacls .\<key>.pem /inheritance:r
icacls .\<key>.pem /grant:r "$($env:USERNAME):(R)"
```

---

## 2. Azure monitoring VM (Ubuntu 24.04)

### 2.1 WireGuard tunnel

```bash
sudo wg show                          # peer, latest handshake, transfer counters
sudo systemctl status wg-quick@wg0    # service state
sudo systemctl restart wg-quick@wg0   # restart the tunnel
ping -c 3 172.16.100.1                # pfSense tunnel address
ping -c 3 10.0.0.2                    # Core-1 through the tunnel
```

### 2.2 Custom application (~/smart-campus-app)

```bash
cd ~/smart-campus-app
docker compose ps                                     # campus-db, campus-worker, campus-web
docker compose logs -f campus-worker                  # live worker log (Ctrl+C to stop)
docker logs campus-worker --tail 30                   # last 30 lines
docker compose exec campus-worker python selftest.py  # connectivity self-test
```

Deploy a new version (upload the archive first, `.env` is kept):

```bash
cd ~ && tar -xzf smart-campus-app.tar.gz
cd ~/smart-campus-app
docker compose up -d --build campus-web campus-worker
```

Reload settings after editing `.env` (a plain restart does not re-read it):

```bash
docker compose up -d --force-recreate campus-worker campus-web
```

Send a test email:

```bash
docker exec campus-worker python -c "from common import notify; print(notify.email('Test email', 'Hello from Smart Campus NOC'))"
```

### 2.3 Application database (campus-db)

```bash
docker exec -it campus-db mysql -u campus -p campus
```

```sql
SHOW TABLES;
SELECT name, ip, status, rtt_ms, last_change FROM devices;
SELECT ts, device, etype, severity, message FROM events ORDER BY id DESC LIMIT 20;
SELECT etype, severity, COUNT(*) AS n FROM events GROUP BY etype, severity ORDER BY n DESC;
SELECT started_at, from_isp, to_isp, ended_at, duration_s FROM failover_events ORDER BY id DESC LIMIT 10;
SELECT ts, device, size_bytes, triggered_by FROM backups ORDER BY id DESC LIMIT 10;
SELECT ts, username, action, target, result FROM audit_log ORDER BY id DESC LIMIT 20;
```

### 2.4 Monitoring stack (~/monitoring)

```bash
cd ~/monitoring
docker compose ps                     # zbx-mysql, zbx-server, zbx-web, grafana
docker compose logs --tail 50 zbx-server
sudo systemctl status zabbix-agent2   # agent on the VM host
```

### 2.5 VM health

```bash
docker stats --no-stream   # CPU and memory per container
free -h                    # memory
df -h                      # disk
uptime                     # load average
timedatectl                # time zone (Asia/Colombo)
```

> **Warning:** never run `docker compose down -v`. The `-v` flag deletes the database
> volumes (all events, backups and Zabbix history).

---

## 3. Core routers (Core-1, Core-2 · VyOS)

Log in as `netadmin` (the default `vyos` account has been removed).

### 3.1 Status

```
show interfaces                         # S/L = admin state / link state
show vrrp                               # MASTER / BACKUP per group
show ip ospf neighbor                   # neighbour should be Full
show ip route                           # O = OSPF, C = connected, S = static
show log vrrp | tail -40                # VRRP state changes with time stamps
show firewall ipv4 forward filter       # rule hit counters (Student -> Admin/Mgmt drops)
show configuration commands | match vrrp
show system memory
show version
```

### 3.2 Change an interface by hand

```
configure
set interfaces ethernet eth1 vif 10 disable      # disable VLAN 10 gateway
commit
save
exit
```

```
configure
delete interfaces ethernet eth1 vif 10 disable   # enable it again
commit
save
exit
```

Discard uncommitted changes: `exit discard`.

---

## 4. ISP routers (ISP-1, ISP-2 · VyOS)

```
show interfaces                    # eth0 = DHCP from GNS3 NAT, eth1 = link to pfSense
show nat source rules              # masquerade rule
show ip route                      # default route + static route to 172.16.100.0/24
ping 8.8.8.8 count 4               # internet reachability from the ISP
ping 203.0.113.2 count 4           # pfSense WAN (ISP-1); use 198.51.100.2 on ISP-2
```

---

## 5. pfSense (web GUI)

| Check | Where |
|---|---|
| Gateway state (WANGW / WAN2_GW) | Status → Gateways |
| Failover decisions with time stamps | Status → System Logs → Gateways |
| WireGuard peer and handshake | VPN → WireGuard → Status |
| Firewall rules and hits | Firewall → Rules → LAN |
| Ping / trace from pfSense | Diagnostics → Ping, Diagnostics → Traceroute |

Gateway monitoring settings in use: probe every 500 ms, 60 s window, down at 20 % loss.

---

## 6. End devices (VPCS in GNS3)

```
show ip                                 # address, mask, gateway
ip 10.10.20.10/24 10.10.20.1            # set address + gateway (Student-PC example)
save                                    # keep it after restart
ping 10.10.30.10 -t                     # continuous ping (Ctrl+C to stop)
ping 8.8.8.8 -t                         # internet through pfSense
trace 8.8.8.8                           # path
```

| Device | Address | Gateway |
|---|---|---|
| Admin-PC | 10.10.10.10/24 | 10.10.10.1 |
| Student-PC | 10.10.20.10/24 | 10.10.20.1 |
| Server | 10.10.30.10/24 | 10.10.30.1 |
| Mgmt-PC | 10.10.99.10/24 | 10.10.99.1 |

---

## 7. Wireshark display filters (GNS3 link capture)

| Link | Filter | Shows |
|---|---|---|
| ISP-1 ↔ pfSense | `udp.port == 51820` | WireGuard handshake and encrypted transport data |
| pfSense ↔ SW-T | `snmp` | SNMPv3 `encryptedPDU` |
| Core-1 ↔ SW-1 | `vrrp` | VRRP advertisements (1 s, priority 200) |
| Core-1 ↔ Core-2 | `ospf` | OSPF Hello packets (10 s) |

---

## 8. Quick troubleshooting order

1. Dashboard shows **SITE UNREACHABLE** → `sudo wg show` on the VM, then pfSense Status → Gateways.
2. One device DOWN → from its VPCS: `show ip`, ping its gateway, ping pfSense `10.0.0.1`, ping `8.8.8.8`.
3. Gateway problems → `show vrrp` and `show log vrrp | tail -40` on both cores.
4. Routing problems → `show ip ospf neighbor` and `show ip route` on both cores.
5. Dashboard action failed → `docker logs campus-web --tail 30` and the "Router output" section in the Interfaces dialog.
