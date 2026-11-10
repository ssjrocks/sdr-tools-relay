# SDR Tools Relay

Lends the RTL-SDR plugged into this PC (Windows or Linux) to
[SDR Toolkit](https://github.com/ssjrocks/sdr-toolkit) on another PC: on the same network, or
over the internet. SDR Toolkit's **Connect relay…** finds it, takes control of the dongle (frequency,
sample rate, gain) and receives its samples, and every RTL-SDR mode (ADS-B, AIS, ISM, LoRa,
DMR/P25, GPS) then runs on it as if it were plugged in locally.

The relay is a small window with a tray icon. It shows the dongle, who's connected, what they've
tuned to and how fast the samples are flowing, with a **Disconnect** button.

## Install

**Windows 10/11:** download `SDR-Tools-Relay-Setup-….exe` from the
[latest release](https://github.com/ssjrocks/sdr-tools-relay/releases/latest) and run it (no
administrator rights needed). The RTL-SDR also needs the WinUSB driver, once: run
[Zadig](https://zadig.akeo.ie), choose Options → List All Devices, pick **Bulk-In, Interface
(Interface 0)** and install **WinUSB**. The first time the relay starts, Windows asks whether to
allow it on networks: allow it (private networks at least), or SDR Toolkit can't reach it. The
installer isn't code-signed, so Windows SmartScreen may warn before running it ("More info" → "Run
anyway").

**Linux** (Ubuntu, Debian, Raspberry Pi OS):

```bash
curl -fsSL https://raw.githubusercontent.com/ssjrocks/sdr-tools-relay/main/scripts/install.sh | bash
```

It installs the RTL-SDR library and Qt from the distribution, and the app in
`~/.local/share/sdr-tools-relay`. `scripts/install.sh --uninstall` removes it.

Both versions update themselves from this repository's releases: **Update to …** appears when there
is one.

## Settings

| Setting | What it does |
|---|---|
| Name | What SDR Toolkit shows in its list |
| Password | Optional. Without one, anyone who can reach the relay can use the dongle |
| Port | TCP port for SDR Toolkit (default 47320). LAN scans use UDP 47321 |
| Frequency correction | This dongle's crystal error in ppm. It's applied to every connection; **Let users change it** lets SDR Toolkit's own value through instead |
| Location | Optional, shown in the public list |
| Reachable from the internet | Opens the port on your router with UPnP, renewed while the relay runs and closed when it stops. The router must have UPnP turned on, or forward the port by hand |
| List it on ssjrocks.com | Puts the relay in the public list SDR Toolkit's **Internet** tab shows |
| Start when I log in | Starts it in the tray at login |

One user at a time: while someone is connected, others are told it's in use.

## Public list and privacy

With **List it on ssjrocks.com** on, the relay tells `https://ssjrocks.com/api/sdr-relay/v1/`
its name, location, port, version, dongle names and whether it has a password, every minute. The
list uses the address the relay connects from, so a relay can only list itself, and it only shows
relays it can actually reach. Anyone with SDR Toolkit can see your relay's internet address and use
your dongle (with the password, if you set one). Turn the option off and the relay leaves the list
within three minutes. The server keeps the list in memory only and logs nothing else. Its source is
in `directory/`.

Nothing is encrypted: the samples are radio anyone nearby can receive. The password never crosses
the network: the relay sends a random challenge and SDR Toolkit answers with an HMAC of it.

## Protocol

After a one-line JSON handshake (the relay's hello with a challenge, the client's answer, the
relay's OK) the connection is plain [rtl_tcp](https://osmocom.org/projects/rtl-sdr/wiki/Rtl-sdr):
the 12-byte `RTL0` header, then 8-bit IQ, with 5-byte commands the other way. See
`src/sdr_relay/protocol.py`. SDR Toolkit runs a local bridge that does the handshake, so its
decoders (rtl_433, readsb, DSD-FME, GNSS-SDR…) talk ordinary rtl_tcp.

At 2.4 MS/s (ADS-B) the stream is about 4.8 MB/s (38 Mbit/s); narrower modes need less (AIS 0.5
MB/s, ISM 0.5 MB/s, LoRa 2 MB/s). If the network can't keep up, the relay drops the oldest
samples rather than falling behind, and the window says how many.

## Problem reports

**Report a problem…** sends your note, the relay's log and its settings (not the password) to the
developer's server. Crashes are sent the same way.

## Tests

```bash
python3 -m pytest tests                              # the server, LAN scan, directory and UPnP, with fakes
QT_QPA_PLATFORM=offscreen python3 tests/gui_smoke.py # the window, on a recording
python3 -m sdr_relay --self-test results.json        # a build on this PC (used by the Windows CI)
```

The relay can also play a recording instead of a dongle: `python3 -m sdr_relay --demo file.cu8`.
SDR Toolkit's `tests/test_relay.py` runs its ISM, ADS-B, AIS and LoRa decoders through this relay
playing their test recordings.

Not yet tested: a real dongle on a relay, Windows on a real PC (the build is self-tested on
GitHub's Windows runner), UPnP on a real router, and connections over the internet.

## Licence

GPL-3.0-or-later. The Windows installer includes `rtlsdr.dll` from
[rtl-sdr-blog](https://github.com/rtlsdrblog/rtl-sdr-blog) (GPL-2.0-or-later), with its runtime
DLLs.
