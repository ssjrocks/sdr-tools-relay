"""The relay protocol (version 1). SDR Toolkit has a copy of the client side (sdr_toolkit/relay.py).

TCP, default port 47320:

1. The relay sends one JSON line, its hello:
   {"relay": "sdr-tools-relay", "protocol": 1, "name", "version", "devices": [...],
    "password": true/false, "busy": true/false, "nonce": "<hex>"}
   A busy relay (someone is connected) closes the connection after it.
2. The client answers with one JSON line:
   {"client": "<app>", "version", "auth": HMAC-SHA256(password, nonce) in hex, or ""}
3. The relay answers {"ok": true, "ppm": N} and carries on, or {"ok": false, "error"} and closes.
4. From then on it's rtl_tcp: the relay sends "RTL0", the tuner type and the number of
   gain steps (big-endian u32 each), then unsigned 8-bit IQ; the client sends 5-byte
   commands (a command byte, a big-endian u32).

LAN discovery: a client sends DISCOVER to UDP port 47321 (broadcast); each relay answers
with its hello (without the nonce) plus "port".

Nothing is encrypted: the samples are radio anyone can receive, and the password
never crosses the network (the HMAC proves it).
"""

import hashlib
import hmac
import json
import struct

PROTOCOL = 1
PORT = 47320
DISCOVERY_PORT = 47321
DISCOVER = b"SDR-TOOLS-RELAY? 1"

# rtl_tcp commands
SET_FREQ, SET_RATE, SET_GAIN_MODE, SET_GAIN, SET_PPM, SET_IF_GAIN, SET_TEST_MODE, SET_AGC, \
    SET_DIRECT_SAMPLING, SET_OFFSET_TUNING, SET_RTL_XTAL, SET_TUNER_XTAL, SET_GAIN_INDEX, SET_BIAS_TEE = range(1, 15)
NAMES = {SET_FREQ: "frequency", SET_RATE: "sample rate", SET_GAIN_MODE: "gain mode", SET_GAIN: "gain",
         SET_PPM: "frequency correction", SET_IF_GAIN: "IF gain", SET_TEST_MODE: "test mode", SET_AGC: "AGC",
         SET_DIRECT_SAMPLING: "direct sampling", SET_OFFSET_TUNING: "offset tuning", SET_RTL_XTAL: "RTL crystal",
         SET_TUNER_XTAL: "tuner crystal", SET_GAIN_INDEX: "gain step", SET_BIAS_TEE: "bias tee"}


def auth(password: str, nonce: str) -> str:
    return hmac.new(password.encode(), nonce.encode(), hashlib.sha256).hexdigest() if password else ""


def check(password: str, nonce: str, given: str) -> bool:
    return not password or hmac.compare_digest(auth(password, nonce), given or "")


def line(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


def header(tuner: int, gain_count: int) -> bytes:
    return b"RTL0" + struct.pack(">II", tuner, gain_count)


def command(cmd: int, value: int) -> bytes:
    return struct.pack(">BI", cmd, value & 0xFFFFFFFF)


def parse_command(data: bytes) -> tuple[int, int]:
    cmd, value = struct.unpack(">BI", data)
    return cmd, value


def signed(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value
