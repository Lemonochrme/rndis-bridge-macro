"""RNDIS host-side protocol (control-plane init + Ethernet data framing) over libusb,
targeting the DJI FPV's interface 0 (control, EP 0x82 IN interrupt) and interface 1
(data, EP 0x81 IN / 0x01 OUT bulk). Reference: Microsoft RNDIS spec (public) / Linux
drivers/net/usb/rndis_host.c (clean, small, public-domain-ish reference implementation).

Only issues standard, non-destructive USB class control requests to bring up a network
link — same as what a normal Windows/Linux RNDIS host driver does automatically on plug-in.
Does not touch the DUML/CDC-ACM interfaces (4/5) or the flight-controller protocol at all.
"""
import struct
import time
import usb.core
import usb.util

VID = 0x2ca3
PID = 0x001f
CTRL_IFACE = 0
DATA_IFACE = 1
NOTIFY_EP = 0x82   # interrupt IN on control interface
DATA_IN_EP = 0x81  # bulk IN on data interface
DATA_OUT_EP = 0x01  # bulk OUT on data interface

# --- RNDIS message types (from the MS-RNDIS spec) ---
REMOTE_NDIS_PACKET_MSG        = 0x00000001
REMOTE_NDIS_INITIALIZE_MSG    = 0x00000002
REMOTE_NDIS_INITIALIZE_CMPLT  = 0x80000002
REMOTE_NDIS_QUERY_MSG         = 0x00000004
REMOTE_NDIS_QUERY_CMPLT       = 0x80000004
REMOTE_NDIS_SET_MSG           = 0x00000005
REMOTE_NDIS_SET_CMPLT         = 0x80000005
REMOTE_NDIS_RESET_MSG         = 0x00000006
REMOTE_NDIS_KEEPALIVE_MSG     = 0x00000008
REMOTE_NDIS_KEEPALIVE_CMPLT   = 0x80000008

OID_GEN_CURRENT_PACKET_FILTER = 0x0001010E
OID_802_3_PERMANENT_ADDRESS   = 0x01010101
OID_802_3_CURRENT_ADDRESS     = 0x01010102

NDIS_PACKET_TYPE_DIRECTED   = 0x00000001
NDIS_PACKET_TYPE_MULTICAST  = 0x00000002
NDIS_PACKET_TYPE_BROADCAST  = 0x00000008
NDIS_PACKET_TYPE_ALL_MCAST  = 0x00000004

# USB CDC class control requests (used to carry the RNDIS control messages over EP0)
SEND_ENCAPSULATED_COMMAND = 0x00
GET_ENCAPSULATED_RESPONSE = 0x01

RID = 0x574C4B31  # arbitrary "request id" tag we reuse/bump per request, spec just wants echo-back


class RndisError(Exception):
    pass


class RndisLink:
    def __init__(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise RndisError("DJI FPV not found on USB — is it plugged in?")
        self._rid = 1

        # Detach anything macOS might have auto-bound (usually nothing binds class 0xE0
        # devices, but be defensive) and claim both RNDIS interfaces.
        for iface in (CTRL_IFACE, DATA_IFACE):
            try:
                if self.dev.is_kernel_driver_active(iface):
                    self.dev.detach_kernel_driver(iface)
            except (usb.core.USBError, NotImplementedError):
                pass
            usb.util.claim_interface(self.dev, iface)

        self.mac = None

    def _next_rid(self):
        self._rid += 1
        return self._rid

    def _ctrl_send(self, payload: bytes):
        # bmRequestType 0x21 = Host->Device | Class | Interface
        self.dev.ctrl_transfer(0x21, SEND_ENCAPSULATED_COMMAND, 0, CTRL_IFACE, payload)

    def _ctrl_recv(self, max_len=1024) -> bytes:
        # bmRequestType 0xA1 = Device->Host | Class | Interface
        return bytes(self.dev.ctrl_transfer(0xA1, GET_ENCAPSULATED_RESPONSE, 0, CTRL_IFACE, max_len))

    def _wait_notify(self, timeout_ms=2000):
        """RNDIS devices signal 'response ready' with an 8-byte packet on the interrupt EP."""
        try:
            self.dev.read(NOTIFY_EP, 16, timeout=timeout_ms)
        except usb.core.USBError as e:
            # Some devices respond fast enough that polling GET_ENCAPSULATED_RESPONSE
            # directly (without waiting for the notification) still works; don't hard-fail here.
            pass

    def _transact(self, msg_type, body: bytes, expect_cmplt, max_resp=1024):
        rid = self._next_rid()
        # Build: MessageType, MessageLength, RequestID, <body...>
        msg = struct.pack("<III", msg_type, 12 + len(body), rid) + body
        self._ctrl_send(msg)
        self._wait_notify()
        resp = self._ctrl_recv(max_resp)
        if len(resp) < 12:
            raise RndisError(f"short RNDIS response ({len(resp)}b) to msg_type=0x{msg_type:08x}")
        r_type, r_len, r_rid = struct.unpack_from("<III", resp, 0)
        if r_type != expect_cmplt:
            raise RndisError(f"unexpected RNDIS response type 0x{r_type:08x}, expected 0x{expect_cmplt:08x}")
        return resp

    def initialize(self):
        # RNDIS_INITIALIZE_MSG body: MajorVersion, MinorVersion, MaxTransferSize
        body = struct.pack("<III", 1, 0, 0x4000)
        resp = self._transact(REMOTE_NDIS_INITIALIZE_MSG, body, REMOTE_NDIS_INITIALIZE_CMPLT)
        status, major, minor, dev_flags, medium, max_pkts, max_xfer = struct.unpack_from("<IIIIIII", resp, 12)
        if status != 0:
            raise RndisError(f"RNDIS initialize failed, status=0x{status:08x}")
        return dict(status=status, major=major, minor=minor, max_transfer=max_xfer)

    def query_oid(self, oid, in_len=0):
        # rndis_query body: Oid, InformationBufferLength, InformationBufferOffset, DeviceVcHandle
        # (4 fields, not 5 — this was miscounted in an earlier draft and shifted everything after it)
        body = struct.pack("<IIII", oid, in_len, 20, 0)
        resp = self._transact(REMOTE_NDIS_QUERY_MSG, body, REMOTE_NDIS_QUERY_CMPLT)
        status, buf_len, buf_off = struct.unpack_from("<III", resp, 12)
        if status != 0:
            raise RndisError(f"RNDIS query OID 0x{oid:08x} failed, status=0x{status:08x}")
        # offset is relative to the RequestID field (byte 8 of the message), per spec/rndis_host.c
        data_start = 8 + buf_off
        return resp[data_start:data_start + buf_len]

    def set_oid(self, oid, value: bytes):
        body = struct.pack("<IIII", oid, len(value), 20, 0) + value
        resp = self._transact(REMOTE_NDIS_SET_MSG, body, REMOTE_NDIS_SET_CMPLT)
        (status,) = struct.unpack_from("<I", resp, 12)
        if status != 0:
            raise RndisError(f"RNDIS set OID 0x{oid:08x} failed, status=0x{status:08x}")

    def bring_up(self):
        info = self.initialize()
        try:
            mac = self.query_oid(OID_802_3_PERMANENT_ADDRESS)
            if len(mac) != 6:
                mac = self.query_oid(OID_802_3_CURRENT_ADDRESS)
        except RndisError:
            mac = self.query_oid(OID_802_3_CURRENT_ADDRESS)
        self.mac = mac
        pkt_filter = struct.pack("<I",
            NDIS_PACKET_TYPE_DIRECTED | NDIS_PACKET_TYPE_MULTICAST |
            NDIS_PACKET_TYPE_BROADCAST | NDIS_PACKET_TYPE_ALL_MCAST)
        self.set_oid(OID_GEN_CURRENT_PACKET_FILTER, pkt_filter)
        return info

    # --- data plane ---
    def send_eth_frame(self, frame: bytes, timeout_ms=1000):
        header = struct.pack("<11I", REMOTE_NDIS_PACKET_MSG, 44 + len(frame), 36, len(frame),
                              0, 0, 0, 0, 0, 0, 0)
        self.dev.write(DATA_OUT_EP, header + frame, timeout=timeout_ms)

    def recv_eth_frame(self, timeout_ms=1000):
        raw = bytes(self.dev.read(DATA_IN_EP, 2048, timeout=timeout_ms))
        if len(raw) < 44:
            return None
        msg_type, msg_len, data_off, data_len = struct.unpack_from("<IIII", raw, 0)
        if msg_type != REMOTE_NDIS_PACKET_MSG:
            return None
        start = 8 + data_off
        return raw[start:start + data_len]

    def close(self):
        for iface in (CTRL_IFACE, DATA_IFACE):
            try:
                usb.util.release_interface(self.dev, iface)
            except usb.core.USBError:
                pass


if __name__ == "__main__":
    link = RndisLink()
    print("Claimed RNDIS interfaces 0+1, initializing link...")
    info = link.bring_up()
    print("RNDIS init OK:", info)
    print("Device MAC:", link.mac.hex(":") if link.mac else None)
    link.close()
