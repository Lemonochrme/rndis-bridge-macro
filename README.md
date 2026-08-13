# dji-rndis-bridge-macos

Userspace RNDIS bridge for updating a **DJI FPV (WM170)** with DJI Assistant 2 on **Apple Silicon macOS**.

Fixes firmware updates stuck at:

```text
Transmitting... 0%
Error: 5-100-4
```

Without using VM or kernel extension. DJI Assistant stays unmodified.

## What this fixes

DJI Assistant downloads the firmware normally, but the transfer to the aircraft never starts.

The reason is that the firmware payload is transferred over **FTP through the aircraft's USB RNDIS interface**. DUML handles control/status traffic, but not the actual firmware payload.

Windows has RNDIS support. macOS does not.

HoRNDIS can provide it on Intel Macs, but doesn't support Apple Silicon.

This project implements the missing RNDIS network path in userspace:

```text
DJI Assistant
    |
    | TCP/IP / FTP
    v
192.168.42.2
    |
   utun
    |
bridge.py
    |
Ethernet
    |
RNDIS
    |
USB bulk transfer
    |
DJI FPV
```

Once the bridge is running, DJI Assistant connects to the aircraft normally and performs the firmware update itself.

## Requirements

* Apple Silicon Mac
* Python 3.9+
* `libusb`
* `pyusb`
* DJI FPV connected over USB-C and powered on

Install dependencies:

```bash
brew install libusb
pip3 install pyusb
```

## Usage

```bash
git clone <this repo>
cd dji-rndis-bridge-macos
sudo python3 bridge.py
```

Wait until you see:

```text
[4/4] Pumping packets
```

Startup normally takes a few seconds while the bridge resolves the aircraft's IP-side MAC address.

Test connectivity:

```bash
ping 192.168.42.2
```

You should get replies.

Then open DJI Assistant 2 and retry the firmware update.

Stop the bridge with `Ctrl-C` when finished.

## Implementation

### `utun.py`

Creates a macOS `utun` interface using:

```text
PF_SYSTEM
SYSPROTO_CONTROL
```

This is the same kernel interface used by userspace VPN software.

The bridge configures the link as:

```text
Mac:   192.168.42.1
Drone: 192.168.42.2
```

No kext is required.

### `rndis.py`

Minimal RNDIS host implementation using `pyusb`.

Handles:

* `REMOTE_NDIS_INITIALIZE_MSG`
* OID queries
* OID sets
* packet filter configuration
* adapter MAC lookup
* Ethernet frames over USB bulk IN/OUT

The packet structures follow the public MS-RNDIS specification and Linux `rndis_host.c`.

### `bridge.py`

Moves packets between the macOS IP stack and the aircraft:

```text
utun packet
    -> Ethernet frame
    -> RNDIS packet
    -> USB bulk OUT
```

and:

```text
USB bulk IN
    -> RNDIS packet
    -> Ethernet frame
    -> utun packet
```

It also performs ARP resolution for the aircraft during startup.

### `probe_usb.py`

Dumps the USB descriptor tree for connected DJI devices using VID:

```text
0x2ca3
```

Useful when checking interface and endpoint numbers on other DJI hardware.

### `verify.py`

Optional read-only connectivity test.

Checks:

* TCP connectivity
* FTP login
* FTP directory listing

It does not upload firmware.

## RNDIS MAC != aircraft MAC

One non-obvious issue:

The MAC returned by the RNDIS `OID_802_3_*` query is **not** the MAC that owns:

```text
192.168.42.2
```

The aircraft appears to have an internal bridge behind the USB RNDIS adapter.

Sending Ethernet frames to the RNDIS adapter's reported MAC results in no response.

`bridge.py` therefore sends an ARP request for:

```text
192.168.42.2
```

and uses the MAC returned by that ARP response.

If you're adapting this to another DJI device and RNDIS initialization succeeds but IP traffic is dead, check this first.

## Supported devices

Confirmed:

```text
DJI FPV / WM170
```

Other DJI devices may use a similar setup, but have not been tested.

## Troubleshooting

### `recv error: No such device`

The aircraft probably rebooted or was power-cycled.

USB re-enumeration invalidates the existing `libusb` handle.

Restart the bridge:

```bash
sudo python3 bridge.py
```

The current process cannot recover the old USB handle.

### ARP resolution fails

Make sure another application is not already holding the RNDIS USB interface.

Only one process can claim the relevant USB interfaces at a time.

Close anything else that may be talking directly to the aircraft over USB, then restart the bridge.

### Run without entering the sudo password every time

You can add a narrowly scoped sudoers rule for this exact command:

```bash
echo "$(whoami) ALL=(root) NOPASSWD: $(which python3) $(pwd)/bridge.py" \
  | sudo tee /etc/sudoers.d/dji-bridge

sudo chmod 440 /etc/sudoers.d/dji-bridge
```

Remove it with:

```bash
sudo rm /etc/sudoers.d/dji-bridge
```

Because the sudoers rule matches the exact command, using `sudo kill` through that rule is not possible.

For scripted shutdown, `bridge.py` watches:

```text
/tmp/dji_bridge_stop
```

Stop it with:

```bash
touch /tmp/dji_bridge_stop
```

## Scope

This project only provides the missing network transport.

It does not implement DJI's firmware protocol and does not upload firmware itself.

DJI Assistant remains responsible for:

* device negotiation
* firmware selection/download
* FTP transfer
* update control
* update status

## Disclaimer

Not affiliated with or endorsed by DJI.

Use at your own risk. Firmware updates can fail if the aircraft loses power, disconnects, or encounters another update error. Keep the aircraft adequately powered during flashing.

## License

MIT. See [LICENSE](LICENSE).
