# dji-rndis-bridge-macos

Fix for DJI FPV (WM170) firmware updates hanging forever at **"Transmitting... 0%"** with
**error 5-100-4**, specifically on **Apple Silicon Macs**. No VM, no kernel extension.

## The problem

If you're here, you probably know the symptom already: DJI Assistant 2 gets
all the way through detecting the aircraft and downloading the firmware, then just sits at
0% forever and eventually fails with `5-100-4`. 

**Root cause:** the DJI FPV's firmware transfer isn't a plain USB bulk transfer. The large
firmware module is sent over **FTP, across a USB RNDIS network link** — DUML (the usual
DJI drone command protocol) only handles the control/handshake/status side. macOS has no
built-in RNDIS host driver, and the common third-party fix for that (HoRNDIS) explicitly
[doesn't support Apple Silicon](https://www.dji.com/support) — DJI's own troubleshooting
docs mention RNDIS for this exact error code on Mac, which is the trail that led here.
Without that network link, the transfer can never start — hence the permanent 0%.

This repo brings up that missing network link entirely in userspace — no kext, no VM, no
waiting on a signed kernel driver — using the same mechanism modern VPN clients
(WireGuard, Tailscale) use for their tunnel interfaces (`utun`, via `PF_SYSTEM`/
`SYSPROTO_CONTROL`.

Once the bridge is running, **the official DJI Assistant works completely unmodified** —
it does its own negotiation and its own FTP transfer, exactly as it does on Windows. This
tool only supplies the network path DJI's app was already expecting to have.

## Requirements

- Apple Silicon Mac (this is also harmless — if a bit pointless — on Intel, where HoRNDIS
  already solves the problem)
- Python 3.9+, [`pyusb`](https://pypi.org/project/pyusb/) + `libusb`:
  ```bash
  brew install libusb
  pip3 install pyusb
  ```
- The DJI FPV aircraft connected via USB-C, powered on

## Quick start

```bash
git clone <this repo>
cd dji-rndis-bridge-macos
sudo python3 bridge.py
```

Wait for `[4/4] Pumping packets` (takes ~5–10s — it actively ARPs for the drone's real
IP-layer MAC, see "Fun bug" below). In another terminal:

```bash
ping 192.168.42.2   # should get replies, ~1-2ms RTT
```

If that works, leave the bridge running and just retry the firmware update in DJI Assistant
as normal. Ctrl-C the bridge once you're done (or see the sudoers section below for
scripting it — plain `kill` won't reach it, more on that below).

## How it works

```
DJI Assistant (unmodified)
        │  connect(192.168.42.2:21), etc.
        ▼
   macOS TCP/IP stack
        │
      utunN  (192.168.42.1 <-> 192.168.42.2, userspace, no kext — same trick WireGuard uses)
        │
┌───────┴────────────────────────────┐
│      bridge.py (this repo)         │
│  utun read()  → wrap in Ethernet    │
│               → RNDIS data packet   │
│               → USB bulk OUT        │
│                                     │
│  USB bulk IN  → unwrap RNDIS/Eth    │
│               → utun write()        │
└───────┬────────────────────────────┘
        ▼
  drone's USB RNDIS interface
```

- `utun.py` — macOS `utun` creation via raw `PF_SYSTEM`/`SYSPROTO_CONTROL` socket calls.
  Needs root only for the final `connect()` step.
- `rndis.py` — RNDIS host-side protocol over `pyusb`: `INITIALIZE`, OID query/set (packet
  filter, MAC address), and the Ethernet-frame data plane over the bulk endpoints. Written
  against the (public) MS-RNDIS spec and cross-checked against Linux's `rndis_host.c`
  struct layouts.
- `bridge.py` — glues the two together, plus just enough hand-rolled ARP to make the link
  usable (see below).
- `probe_usb.py` — dumps the full USB descriptor tree of any connected DJI device (VID
  `0x2ca3`) so you can confirm interface/endpoint numbers if you're adapting this for a
  different DJI product.
- `verify.py` — optional read-only sanity check (TCP connect + FTP login + directory
  listing, no upload) once the bridge is up.

### Fun bug found along the way

The RNDIS adapter's own MAC address (what you get back from an `OID_802_3_*` query) is
**not** the MAC that actually answers for `192.168.42.2` — there's an internal bridge on
the drone's side, and the two differ. Framing outgoing packets to the RNDIS-adapter MAC
produces total silence (frames delivered to the wrong node, silently dropped — no ARP,
no ICMP, nothing). `bridge.py` actively ARPs for `192.168.42.2` on startup and uses
*that* MAC instead. If you're adapting this for another device and get dead silence even
though RNDIS `INITIALIZE` succeeds, check this first.

## Which devices this might help

Confirmed working: **DJI FPV (WM170)**.

## Troubleshooting

**Bridge starts but nothing responds (`recv error: No such device`, repeated) after it was
briefly working** — the aircraft power-cycled (battery swap, reboot) and USB re-enumerated;
the running bridge process is holding a now-invalid `libusb` handle. Stop it and relaunch —
don't just wait, it won't recover on its own.

**Running the bridge repeatedly and don't want a password prompt every time** — a narrowly
scoped `sudoers` rule works and doesn't expose anything beyond "run exactly this script as
root, no other privilege":
```bash
echo "$(whoami) ALL=(root) NOPASSWD: $(which python3) $(pwd)/bridge.py" | sudo tee /etc/sudoers.d/dji-bridge
sudo chmod 440 /etc/sudoers.d/dji-bridge
```
Because the rule matches the literal command line with no extra arguments, you can't pass
`kill` through it and env vars won't reach the process either — that's why `bridge.py`
watches for a `/tmp/dji_bridge_stop` marker file to shut down cleanly instead
(`touch /tmp/dji_bridge_stop`), and why `--quiet` is a flag on the script rather than an
env var. Remove the rule any time with `sudo rm /etc/sudoers.d/dji-bridge`.

**ARP resolution keeps failing** — make sure DJI Assistant / DJI Fly isn't already holding
the USB connection open elsewhere; only one process can claim the RNDIS interfaces at a
time.

## Disclaimer

Not affiliated with or endorsed by DJI. Provided as-is; you are responsible for your own
hardware. This only establishes a network link — it doesn't send anything to your aircraft
beyond standard ARP/ICMP and whatever DJI's own official software sends once it can reach
`192.168.42.2`. Still, flashing firmware always carries some risk; make sure your aircraft
has good battery/power throughout the update regardless of which tool you use.

## License

MIT see [LICENSE](LICENSE).
