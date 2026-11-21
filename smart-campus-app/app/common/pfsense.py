"""pfSense access through its built-in XML-RPC interface (the same API used for HA sync).

Requires only the admin credentials; no extra package on pfSense.
"""
import ssl
import urllib.parse
import xmlrpc.client

from . import config

DOWN_STATES = {"down", "force_down"}

_GATEWAY_PHP = r'''
require_once("gwlb.inc");
$toreturn = array();
foreach (return_gateways_status(true) as $name => $gw) {
    $toreturn[$name] = array(
        "status" => strval($gw["status"]),
        "delay" => strval($gw["delay"]),
        "loss" => strval($gw["loss"]),
        "monitorip" => strval($gw["monitorip"]));
}
'''

_CONFIG_PHP = r'''$toreturn = file_get_contents("/cf/conf/config.xml");'''


class _TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout=10):
        super().__init__(context=ssl._create_unverified_context())  # self-signed GUI cert
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _proxy():
    user = urllib.parse.quote(config.PFSENSE_USER, safe="")
    pwd = urllib.parse.quote(config.PFSENSE_PASS, safe="")
    url = f"https://{user}:{pwd}@{config.PFSENSE_HOST}/xmlrpc.php"
    return xmlrpc.client.ServerProxy(url, transport=_TimeoutTransport())


def gateway_status():
    """Return {gateway_name: {status, delay, loss, monitorip}}."""
    result = _proxy().pfsense.exec_php(_GATEWAY_PHP)
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected reply from pfSense: {result!r}")
    return result


def is_up(gw):
    return gw is not None and gw.get("status", "down") not in DOWN_STATES


def active_isp(gws):
    """Mirror the ISP_FAILOVER group logic: Tier 1 (ISP-1) unless it is down."""
    if is_up(gws.get("WANGW")):
        return "ISP-1"
    if is_up(gws.get("WAN2_GW")):
        return "ISP-2"
    return "NONE"


def config_xml():
    return _proxy().pfsense.exec_php(_CONFIG_PHP)
