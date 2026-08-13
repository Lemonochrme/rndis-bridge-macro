#!/usr/bin/env python3
"""RNDIS-over-USB <-> utun bridge — no kext, no VM.

Some DJI aircraft (confirmed: DJI FPV / WM170; the same firmware-update code path is also
used by gl170, wm162, pm430, pm320m per the compatibility table of a third-party firmware
tool — untested on those, feedback welcome) transfer their firmware over FTP across a USB
RNDIS network link rather than as a plain USB bulk transfer. macOS has no built-in RNDIS
host driver, and the usual third-party fix (HoRNDIS) doesn't support Apple Silicon. This
script brings up that network link entirely in userspace: no kernel extension, no VM.

It pumps Ethernet frames between the drone's USB RNDIS data endpoints and a macOS `utun`
interface (the same no-kext mechanism WireGuard/Tailscale use for their tunnels),
translating Ethernet <-> raw IP at the boundary. utun itself is L3-only, but the drone's
side is a real Ethernet-emulation stack, so it still needs to ARP-resolve our MAC before it
can address a reply back to us — we implement just enough ARP for that ourselves.

Must be run as root (utun creation requires it):
    sudo python3 bridge.py

Ctrl-C to tear down. This only brings up the network link — it does not send any
DUML/upgrade commands and does not touch the firmware/upload protocol at all. Point your
own tooling (or the official DJI Assistant) at 192.168.42.2 once it's up.
"""
import argparse
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import usb.core

sys.stdout.reconfigure(line_buffering=True)  # so tailing a redirected log file works live

from rndis import RndisLink
from utun import Utun

HOST_IP = "192.168.42.1"
DRONE_IP = "192.168.42.2"
HOST_MAC = bytes.fromhex("02deadbeef01")  # locally-administered, arbitrary — only used on the USB link
ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86DD
ETHERTYPE_ARP = 0x0806
ARP_HTYPE_ETHERNET = 1
ARP_OP_REQUEST = 1
ARP_OP_REPLY = 2

VERBOSE = True  # toggled by --quiet


def log(msg):
    if VERBOSE:
        print(msg)


def eth_wrap(dst_mac: bytes, src_mac: bytes, ethertype: int, payload: bytes) -> bytes:
    return dst_mac + src_mac + struct.pack("!H", ethertype) + payload


def eth_unwrap(frame: bytes):
    if len(frame) < 14:
        return None, None
    dst, src, ethertype = frame[0:6], frame[6:12], struct.unpack("!H", frame[12:14])[0]
    return ethertype, frame[14:]


def parse_arp(payload: bytes):
    """Returns (opcode, sender_mac, sender_ip, target_mac, target_ip) or None if malformed."""
    if len(payload) < 28:
        return None
    htype, ptype, hlen, plen, opcode = struct.unpack_from("!HHBBH", payload, 0)
    if htype != ARP_HTYPE_ETHERNET or ptype != ETHERTYPE_IPV4 or hlen != 6 or plen != 4:
        return None
    sender_mac = payload[8:14]
    sender_ip = socket.inet_ntoa(payload[14:18])
    target_mac = payload[18:24]
    target_ip = socket.inet_ntoa(payload[24:28])
    return opcode, sender_mac, sender_ip, target_mac, target_ip


def build_arp(opcode, sender_mac, sender_ip, target_mac, target_ip):
    return (struct.pack("!HHBBH", ARP_HTYPE_ETHERNET, ETHERTYPE_IPV4, 6, 4, opcode)
            + sender_mac + socket.inet_aton(sender_ip)
            + target_mac + socket.inet_aton(target_ip))


def resolve_arp(link, target_ip, sender_ip, sender_mac, retries=5, timeout_s=1.0):
    """Actively ARP for target_ip and return its MAC, or None after `retries` attempts.

    This is not optional: on the DJI FPV, the RNDIS adapter's own MAC (what you get back
    from an RNDIS OID query) and the MAC that actually answers for the drone's IP are
    DIFFERENT — there's an internal bridge on the drone's side. Skipping this and just using
    the RNDIS-adapter MAC as the destination results in total silence (frames delivered to
    the wrong L2 address, silently dropped)."""
    broadcast = b"\xff\xff\xff\xff\xff\xff"
    for attempt in range(1, retries + 1):
        req = build_arp(ARP_OP_REQUEST, sender_mac, sender_ip, b"\x00" * 6, target_ip)
        link.send_eth_frame(eth_wrap(broadcast, sender_mac, ETHERTYPE_ARP, req))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                frame = link.recv_eth_frame(timeout_ms=200)
            except Exception:
                continue
            if not frame:
                continue
            ethertype, payload = eth_unwrap(frame)
            if ethertype != ETHERTYPE_ARP:
                continue
            parsed = parse_arp(payload)
            if parsed and parsed[0] == ARP_OP_REPLY and parsed[2] == target_ip:
                return parsed[1]
        log(f"      (attempt {attempt}/{retries}: no ARP reply yet, retrying...)")
    return None


PROTO_NAMES = {1: "ICMP", 6: "TCP", 17: "UDP"}
ICMP_TYPE_NAMES = {0: "echo-reply", 8: "echo-request", 3: "dest-unreachable", 11: "time-exceeded"}


def describe_ip(payload: bytes) -> str:
    """Best-effort one-line IPv4 summary for debug logging. Never raises."""
    try:
        if len(payload) < 20 or (payload[0] >> 4) != 4:
            return f"non-IPv4/short ({len(payload)}b): {payload[:16].hex()}"
        ihl = (payload[0] & 0x0F) * 4
        total_len = struct.unpack_from("!H", payload, 2)[0]
        proto = payload[9]
        src = socket.inet_ntoa(payload[12:16])
        dst = socket.inet_ntoa(payload[16:20])
        proto_name = PROTO_NAMES.get(proto, f"proto{proto}")
        extra = ""
        if proto == 1 and len(payload) >= ihl + 8:
            icmp_type, icmp_code = payload[ihl], payload[ihl + 1]
            icmp_id, icmp_seq = struct.unpack_from("!HH", payload, ihl + 4)
            extra = (f" {ICMP_TYPE_NAMES.get(icmp_type, f'type{icmp_type}')} "
                     f"code={icmp_code} id={icmp_id} seq={icmp_seq}")
        elif proto in (6, 17) and len(payload) >= ihl + 4:
            sport, dport = struct.unpack_from("!HH", payload, ihl)
            extra = f" sport={sport} dport={dport}"
        return f"{src} -> {dst} {proto_name} len={total_len} (actual {len(payload)}b){extra}"
    except Exception as e:
        return f"<parse error: {e}> {payload[:16].hex()}"


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="suppress per-packet logging (keep stage/stats lines)")
    args = ap.parse_args()
    VERBOSE = not args.quiet

    if os.geteuid() != 0:
        sys.exit("Must run as root (utun creation needs it): sudo python3 bridge.py")

    print("[1/4] Bringing up RNDIS link over USB...")
    link = RndisLink()
    info = link.bring_up()
    print(f"      RNDIS OK: {info}, RNDIS adapter MAC={link.mac.hex(':') if link.mac else '?'}")

    print("[1b/4] Resolving drone's real IP-layer MAC via ARP "
          "(it differs from the RNDIS adapter MAC — internal bridge on the drone's side)...")
    drone_mac = resolve_arp(link, DRONE_IP, HOST_IP, HOST_MAC)
    if drone_mac is None:
        sys.exit(f"      Could not ARP-resolve {DRONE_IP} — no reply after retries. "
                  f"Is the drone's system actually up? (try again, or increase retries)")
    print(f"      {DRONE_IP} is-at {drone_mac.hex(':')}")

    print("[2/4] Creating utun interface...")
    tun = Utun()
    print(f"      Created {tun.name}")

    print("[3/4] Configuring IP + route...")
    subprocess.run(["ifconfig", tun.name, HOST_IP, DRONE_IP, "up"], check=True)
    print(f"      {tun.name}: {HOST_IP} <-> {DRONE_IP}")

    print("[4/4] Pumping packets (Ctrl-C to stop)...")
    stop = threading.Event()
    stats = {"utun_out": 0, "usb_in": 0, "arp_replied": 0, "ip_in": 0, "dropped": 0}

    def utun_to_usb():
        while not stop.is_set():
            try:
                pkt = tun.read()
            except OSError:
                break
            if not pkt:
                continue
            ethertype = ETHERTYPE_IPV6 if (pkt[0] >> 4) == 6 else ETHERTYPE_IPV4
            frame = eth_wrap(drone_mac, HOST_MAC, ethertype, pkt)
            log(f"  [utun->usb] {describe_ip(pkt)}")
            try:
                link.send_eth_frame(frame)
                stats["utun_out"] += 1
            except Exception as e:
                print(f"  [utun->usb] send error: {e}")

    def usb_to_utun():
        while not stop.is_set():
            try:
                frame = link.recv_eth_frame(timeout_ms=1000)
            except usb.core.USBTimeoutError:
                continue
            except Exception as e:
                print(f"  [usb->utun] recv error ({type(e).__name__}): {e}")
                continue
            if not frame:
                continue
            stats["usb_in"] += 1
            ethertype, payload = eth_unwrap(frame)
            if ethertype is None:
                log(f"  [usb->utun] short/malformed frame ({len(frame)}b): {frame[:32].hex()}")
                stats["dropped"] += 1
                continue
            if ethertype in (ETHERTYPE_IPV4, ETHERTYPE_IPV6):
                stats["ip_in"] += 1
                log(f"  [usb->utun] {describe_ip(payload)}")
                try:
                    tun.write(payload)
                except OSError as e:
                    print(f"  [usb->utun] write error: {e}")
            elif ethertype == ETHERTYPE_ARP:
                parsed = parse_arp(payload)
                if parsed:
                    opcode, sender_mac, sender_ip, target_mac, target_ip = parsed
                    log(f"  [ARP] {'request' if opcode == ARP_OP_REQUEST else 'reply'} "
                        f"who-has {target_ip}? tell {sender_ip} ({sender_mac.hex(':')})")
                    if opcode == ARP_OP_REQUEST and target_ip == HOST_IP:
                        reply = build_arp(ARP_OP_REPLY, HOST_MAC, HOST_IP, sender_mac, sender_ip)
                        link.send_eth_frame(eth_wrap(sender_mac, HOST_MAC, ETHERTYPE_ARP, reply))
                        stats["arp_replied"] += 1
                        log(f"  [ARP] replied: {HOST_IP} is-at {HOST_MAC.hex(':')}")
            else:
                log(f"  [usb->utun] dropped, ethertype=0x{ethertype:04x} ({len(payload)}b payload)")
                stats["dropped"] += 1

    t1 = threading.Thread(target=utun_to_usb, daemon=True)
    t2 = threading.Thread(target=usb_to_utun, daemon=True)
    t1.start()
    t2.start()

    # If you're running this under a scoped `sudo ... NOPASSWD` rule (recommended — see
    # README) rather than an interactive sudo, a plain `kill` from an unprivileged helper
    # can't reach this root-owned process. A stop-file sidesteps that without needing any
    # extra sudo grant, and doubles as a clean way to script this from another program.
    stop_file = "/tmp/dji_bridge_stop"
    try:
        if os.path.exists(stop_file):
            os.remove(stop_file)
    except OSError:
        pass
    try:
        last = dict(stats)
        while not os.path.exists(stop_file):
            time.sleep(1)
            if stats != last:
                print(f"  stats: {stats}")
                last = dict(stats)
    except KeyboardInterrupt:
        pass
    print("\nShutting down...")
    stop.set()
    tun.close()
    link.close()
    try:
        os.remove(stop_file)
    except OSError:
        pass


if __name__ == "__main__":
    main()
