"""The relay protocol (version 1). SDR Toolkit has a copy of the client side (sdr_toolkit/relay.py).

TCP, default port 47320:

1. The relay sends one JSON line, its hello:
   {"relay": "sdr-tools-relay", "protocol": 1, "name", "version", "devices": [...],
    "password": true/false, "busy": true/false, "nonce": "<hex>"}
   A busy relay (someone is connected) closes the connection after it.
2. The client answers with one JSON line:
   {"client": "<app>", "version", "auth": HMAC-SHA256(password, nonce) in hex, or ""}
   With "check": true it only checks the password: the relay answers {"ok": true,
   "checked": true} and closes without opening the dongle.
3. The relay answers {"ok": true, "ppm": N} and carries on, or {"ok": false, "error"} and closes.
   A connection arriving while the previous client's dongle is still closing waits for it.
   With "update": true (when the hello's "features" has "update") the relay looks for a newer
   release and, if there is one, answers {"ok": true, "updating": "<version>"}, closes, and
   installs it (then restarts). Only with the password: a relay without one refuses.
   With "stats": true (password only) the relay answers {"ok": true, "stats": {...}} and closes, even
   while someone is connected (then the hello says busy, and this is the only request it answers):
   {"device", "freq", "rate", "gain", "needed_bps" (2 bytes a sample), "device_bps" (from the device,
   its USB link), "net_bps" (to the client), "device_bytes", "net_bytes", "dropped", "seconds"}, or
   null when nobody is. The client times it for the network's round trip.
   With "diagnose": true (also password only) the relay tries each way of reading its dongle
   for 2 s and answers {"ok": true, "report": {...}} (see diagnose.py); it takes ~20 s.
4. From then on it's rtl_tcp: the relay sends "RTL0", the tuner type and the number of
   gain steps (big-endian u32 each), then unsigned 8-bit IQ; the client sends 5-byte
   commands (a command byte, a big-endian u32).

Devices. The hello's "devices" lists each one: {"id": "rtl:0" | "hackrf:<serial>", "type": "rtl" |
"hackrf", "name", "serial"}. The client's reply may name one ("device": id); otherwise the relay's
own choice is used. The "ok" answer then carries "device": what it is ({"type": "hackrf", "board",
"firmware", "usb_api", "serial", "min_rate", "max_rate", "min_freq", "max_freq", ...}).

A HackRF (features "hackrf"):
- streams like an RTL-SDR (step 4): rtl_tcp's tuner type is 100; its 52 gain steps are every LNA +
  VGA total, 0-102 dB in 2 dB; the bias tee is antenna power; sample rates are 2-20 MS/s (a client
  that wants less asks for a whole multiple and filters it down itself; from relay 0.3.0, features
  "narrow", it may ask for any rate: the relay samples at a whole multiple of 2 MS/s or more and
  filters it down). Its own commands:
  SET_FREQ_HI (0x40, the upper 32 bits of the next SET_FREQ: frequencies to 6 GHz), SET_LNA_GAIN
  (0x41, dB 0-40), SET_VGA_GAIN (0x42, dB 0-62), SET_AMP (0x43, RF amp 0/1), SET_ANTENNA_POWER
  (0x44, 0/1), SET_BASEBAND_FILTER (0x45, Hz, 0 = automatic), SET_CLKOUT (0x46, 0/1),
  SET_TXVGA_GAIN (0x47, dB 0-47, for transmitting), SET_DC_BLOCK (0x48, 0/1: features "dc_block",
  on unless turned off: the HackRF's DC spike at the centre taken out before the samples are sent).
- the stats answer (above) adds, for a HackRF (features "levels"): "level" {"dbfs" (average power,
  hackrf_transfer's dB full scale), "peak" (0-128), "clipped" (the share of samples at the ADC's
  limit)} over the last second of the full-rate samples, "usb_lost" (the HackRF's own count of
  overruns, when its firmware reports them), "device_rate" (what it samples at) and "narrow" (the
  decimation to the client's rate).
- sweeps (features "sweep"): {"stream": "sweep", "ranges": [[lo_mhz, hi_mhz], ...] (up to 10),
  "lna", "vga", "amp", "antenna"} gets {"ok": true, "stream": "sweep", "ranges": [lo, hi, ...]}
  (rounded up to 20 MHz steps, as hackrf_sweep does), then hackrf_sweep's raw transfers: 16384-byte
  blocks, each 0x7F 0x7F, the block's frequency in Hz (u64, little-endian) and signed 8-bit IQ.
  The client does the FFTs. With "fft_size": N (features "sweep_trim"), each block is cut to its
  header and its last N samples, all hackrf_sweep's FFT reads (10 + 2N bytes).
- transmits (features "tx", only when the relay's owner allows it, and only with a password):
  {"stream": "tx", "freq", "rate", "txvga", "amp", "antenna", "filter"} gets {"ok": true, "stream":
  "tx"}; then everything the client sends is signed 8-bit IQ to transmit, until it disconnects.

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
# HackRF commands (see above)
SET_FREQ_HI, SET_LNA_GAIN, SET_VGA_GAIN, SET_AMP, SET_ANTENNA_POWER, SET_BASEBAND_FILTER, SET_CLKOUT, \
    SET_TXVGA_GAIN, SET_DC_BLOCK = range(0x40, 0x49)
NAMES = {SET_FREQ: "frequency", SET_RATE: "sample rate", SET_GAIN_MODE: "gain mode", SET_GAIN: "gain",
         SET_PPM: "frequency correction", SET_IF_GAIN: "IF gain", SET_TEST_MODE: "test mode", SET_AGC: "AGC",
         SET_DIRECT_SAMPLING: "direct sampling", SET_OFFSET_TUNING: "offset tuning", SET_RTL_XTAL: "RTL crystal",
         SET_TUNER_XTAL: "tuner crystal", SET_GAIN_INDEX: "gain step", SET_BIAS_TEE: "bias tee",
         SET_FREQ_HI: "frequency (upper bits)", SET_LNA_GAIN: "LNA gain", SET_VGA_GAIN: "VGA gain",
         SET_AMP: "RF amp", SET_ANTENNA_POWER: "antenna power", SET_BASEBAND_FILTER: "baseband filter",
         SET_CLKOUT: "clock out", SET_TXVGA_GAIN: "TX gain", SET_DC_BLOCK: "DC spike removal"}


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
