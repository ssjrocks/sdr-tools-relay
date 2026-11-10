"""Listing the relay on ssjrocks.com, where SDR Toolkit's "Internet" tab finds it.

The relay registers every minute; the directory uses the address the request came
from (a relay can only list itself), checks that it can reach the port, and drops
relays it hasn't heard from for 3 minutes. See directory/ in this repo for the server.
"""

import json
import logging
import urllib.request

log = logging.getLogger(__name__)
URL = "https://ssjrocks.com/api/sdr-relay/v1"
EVERY = 60


def _post(url, body: dict, user_agent: str, timeout=15) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def register(relay, relay_id: str, location: str, user_agent: str, base=URL) -> dict:
    """{"listed", "reachable", "address"} from the directory. Raises OSError / ValueError."""
    hello = relay.hello()
    body = {"id": relay_id, "name": hello["name"], "port": relay.config.port, "location": location,
            "version": hello["version"], "password": hello["password"],
            "devices": [d.get("name", "") for d in hello["devices"]]}
    return _post(f"{base}/register", body, user_agent)


def unregister(relay_id: str, user_agent: str, base=URL):
    try:
        _post(f"{base}/unregister", {"id": relay_id}, user_agent, timeout=5)
    except (OSError, ValueError) as e:
        log.info("unregister: %s", e)
