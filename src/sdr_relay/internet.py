"""Keeps the relay reachable from the internet and listed: the UPnP port mapping and the directory."""

import logging
import threading

from . import directory
from .upnp import LEASE, PortMapping, UPnPError

log = logging.getLogger(__name__)


class Internet:
    def __init__(self, relay, relay_id: str, user_agent: str):
        self.relay = relay
        self.relay_id = relay_id
        self.user_agent = user_agent
        self.open_port = False
        self.listed = False
        self.location = ""
        self.port_status = "Not reachable from the internet (turned off)"
        self.list_status = "Not listed"
        self._mapping: PortMapping | None = None
        self._was_listed = False
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="relay-internet")

    def start(self):
        self._thread.start()

    def configure(self, open_port: bool, listed: bool, location: str):
        self.open_port, self.listed, self.location = open_port, listed, location
        self._wake.set()

    def port_changed(self):
        self._close_mapping()
        self._wake.set()

    def stop(self):
        self._halt.set()
        self._wake.set()
        self._thread.join(10)
        self._close_mapping()
        if self._was_listed:
            directory.unregister(self.relay_id, self.user_agent)

    def _close_mapping(self):
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None

    def _run(self):
        renew_in = 0.0
        while not self._halt.is_set():
            # The port on the router.
            if self.open_port:
                try:
                    if self._mapping is None or self._mapping.port != self.relay.config.port:
                        self._close_mapping()
                        m = PortMapping(self.relay.config.port)
                        m.open()
                        self._mapping = m
                        renew_in = LEASE / 2
                    elif renew_in <= 0:
                        self._mapping.renew()
                        renew_in = LEASE / 2
                    self.port_status = (f"Port {self.relay.config.port} is open on the router "
                                        f"(internet address {self._mapping.external_ip or 'unknown'})")
                except (UPnPError, OSError) as e:
                    self._mapping = None
                    self.port_status = f"Couldn't open the port on the router: {e}"
                    log.info(self.port_status)
            else:
                self._close_mapping()
                self.port_status = "Not reachable from the internet (turned off)"
            # The public list.
            if self.listed:
                try:
                    got = directory.register(self.relay, self.relay_id, self.location, self.user_agent)
                    self._was_listed = True
                    if got.get("listed"):
                        self.list_status = f"Listed on ssjrocks.com as {got.get('address')}:{self.relay.config.port}"
                    else:
                        self.list_status = (f"Not listed: ssjrocks.com couldn't reach {got.get('address')}:"
                                            f"{self.relay.config.port} (is the port open on the router?)")
                except (OSError, ValueError) as e:
                    self.list_status = f"Couldn't reach ssjrocks.com: {e}"
                    log.info(self.list_status)
            elif self._was_listed:
                directory.unregister(self.relay_id, self.user_agent)
                self._was_listed = False
                self.list_status = "Not listed"
            else:
                self.list_status = "Not listed"
            self._wake.wait(directory.EVERY)
            self._wake.clear()
            renew_in -= directory.EVERY
