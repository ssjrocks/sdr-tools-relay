"""Opening the relay's port on the home router with UPnP IGD, so SDR Toolkit can reach it from the internet.

Finds the router with SSDP, reads its description, and calls AddPortMapping /
GetExternalIPAddress / DeletePortMapping on its WANIPConnection (or WANPPPConnection)
service. The mapping is renewed while the relay runs and removed when it stops.
"""

import logging
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin
from xml.etree import ElementTree

log = logging.getLogger(__name__)
SSDP = ("239.255.255.250", 1900)
SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANPPPConnection:1")
LEASE = 3600


class UPnPError(RuntimeError):
    pass


def discover(timeout=2.0) -> list[str]:
    """Description URLs of internet gateways answering on the LAN."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    found = []
    try:
        s.sendto(msg, SSDP)
        while True:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m and m.group(1).decode() not in found:
                found.append(m.group(1).decode())
    finally:
        s.close()
    return found


def _local_ip_towards(url) -> str:
    host = re.match(r"https?://([^:/]+)", url).group(1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 1900))
        return s.getsockname()[0]
    finally:
        s.close()


def control_url(description_url: str, xml: bytes) -> tuple[str, str]:
    """(control URL, service type) of the WAN connection service in a device description."""
    root = ElementTree.fromstring(xml)
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    base = root.findtext("d:URLBase", default="", namespaces=ns) or description_url
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        stype = svc.findtext("d:serviceType", default="", namespaces=ns)
        if stype in SERVICES:
            return urljoin(base, svc.findtext("d:controlURL", default="", namespaces=ns)), stype
    raise UPnPError("the router has no WAN connection service")


def soap(url, service, action, args: dict, timeout=5.0) -> dict:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    envelope = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    req = urllib.request.Request(url, data=envelope, headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            xml = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()
        m = re.search(rb"<errorDescription>(.*?)</errorDescription>", detail)
        raise UPnPError(f"{action}: {m.group(1).decode() if m else e}") from None
    except OSError as e:
        raise UPnPError(f"{action}: {e}") from None
    out = {}
    for el in ElementTree.fromstring(xml).iter():
        tag = el.tag.split("}")[-1]
        if el.text is not None and not list(el):
            out[tag] = el.text
    return out


class PortMapping:
    def __init__(self, port: int, description="SDR Tools Relay"):
        self.port = port
        self.description = description
        self.url = self.service = None
        self.external_ip = ""
        self.internal_ip = ""

    def open(self, description_url: str | None = None):
        """Find the router and map the port to this PC. Raises UPnPError with the reason."""
        urls = [description_url] if description_url else discover()
        if not urls:
            raise UPnPError("no router answered UPnP (it may be turned off in the router's settings)")
        errors = []
        for loc in urls:
            try:
                with urllib.request.urlopen(loc, timeout=5) as r:
                    self.url, self.service = control_url(loc, r.read())
                self.internal_ip = _local_ip_towards(self.url)
                self.renew()
                self.external_ip = soap(self.url, self.service, "GetExternalIPAddress", {}).get(
                    "NewExternalIPAddress", "")
                return
            except (UPnPError, OSError, ElementTree.ParseError) as e:
                errors.append(str(e))
        raise UPnPError("; ".join(errors))

    def renew(self):
        soap(self.url, self.service, "AddPortMapping", {
            "NewRemoteHost": "", "NewExternalPort": self.port, "NewProtocol": "TCP",
            "NewInternalPort": self.port, "NewInternalClient": self.internal_ip, "NewEnabled": 1,
            "NewPortMappingDescription": self.description, "NewLeaseDuration": LEASE})

    def close(self):
        if self.url:
            try:
                soap(self.url, self.service, "DeletePortMapping",
                     {"NewRemoteHost": "", "NewExternalPort": self.port, "NewProtocol": "TCP"})
            except UPnPError as e:
                log.info("removing the port mapping: %s", e)
            self.url = None
