"""Minimal macOS utun (PF_SYSTEM/SYSPROTO_CONTROL) wrapper — no kext required.
Same mechanism used by WireGuard/Tailscale/every modern macOS VPN client userspace.
Reads/writes carry a 4-byte AF family prefix (network byte order) before the raw IP packet.

Uses Python's stdlib socket/fcntl for socket creation and the CTLIOCGINFO ioctl (both are
well-marshaled by CPython's own wrappers); only `connect()` needs a raw ctypes call since
Python's socket.connect() doesn't know the PF_SYSTEM sockaddr_ctl layout.
"""
import ctypes
import ctypes.util
import fcntl
import os
import socket
import struct

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
libc.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
libc.connect.restype = ctypes.c_int

AF_SYSTEM = 32
AF_SYS_CONTROL = 2
UTUN_OPT_IFNAME = 2
CTLIOCGINFO = 0xC0644E03  # _IOWR('N', 3, struct ctl_info)  (struct is 100 bytes: u32 + char[96])

class sockaddr_ctl(ctypes.Structure):
    _fields_ = [
        ("sc_len", ctypes.c_uint8),
        ("sc_family", ctypes.c_uint8),
        ("ss_sysaddr", ctypes.c_uint16),
        ("sc_id", ctypes.c_uint32),
        ("sc_unit", ctypes.c_uint32),
        ("sc_reserved", ctypes.c_uint32 * 5),
    ]

class Utun:
    def __init__(self, unit=0):
        self.sock = socket.socket(socket.PF_SYSTEM, socket.SOCK_DGRAM, socket.SYSPROTO_CONTROL)
        fd = self.sock.fileno()

        # struct ctl_info { u_int32_t ctl_id; char ctl_name[96]; };
        buf = bytearray(struct.pack("<I", 0) + b"com.apple.net.utun_control".ljust(96, b"\x00"))
        fcntl.ioctl(fd, CTLIOCGINFO, buf, True)  # mutate_flag=True: fills buf in place
        ctl_id = struct.unpack("<I", buf[:4])[0]

        # sc_unit=0 asks the kernel to assign the first free utunN dynamically. A fixed
        # unit (N+1) would collide with utun1/2/3 etc. already held by iCloud Private
        # Relay, other VPNs, etc. — that's what caused the earlier EBUSY.
        requested_units = [0] if unit == 0 else [unit + 1]
        last_err = None
        for sc_unit in requested_units:
            addr = sockaddr_ctl(
                sc_len=ctypes.sizeof(sockaddr_ctl),
                sc_family=AF_SYSTEM,
                ss_sysaddr=AF_SYS_CONTROL,
                sc_id=ctl_id,
                sc_unit=sc_unit,
            )
            r = libc.connect(fd, ctypes.byref(addr), ctypes.sizeof(addr))
            if r < 0:
                last_err = ctypes.get_errno()
                continue
            last_err = None
            break
        if last_err is not None:
            raise OSError(last_err, f"connect(utun) failed: {os.strerror(last_err)}")

        name = self.sock.getsockopt(socket.SYSPROTO_CONTROL, UTUN_OPT_IFNAME, 64)
        self.name = name.split(b"\x00", 1)[0].decode()
        self.fd = fd

    def read(self, size=4096):
        buf = os.read(self.fd, size)
        if len(buf) < 4:
            return b""
        return buf[4:]  # strip 4-byte AF family prefix

    def write(self, ip_packet: bytes):
        version = ip_packet[0] >> 4
        af = socket.AF_INET if version == 4 else socket.AF_INET6
        prefix = struct.pack("!I", af)
        return os.write(self.fd, prefix + ip_packet)

    def close(self):
        self.sock.close()


if __name__ == "__main__":
    u = Utun()
    print(f"Created {u.name} (fd={u.fd})")
    print(f"  sudo ifconfig {u.name} 192.168.42.1 192.168.42.2 up")
    print("Ctrl-C to exit (interface disappears when fd closes).")
    try:
        while True:
            pkt = u.read()
            if pkt:
                print(f"IN  {len(pkt)}b: {pkt[:40].hex()}")
    except KeyboardInterrupt:
        pass
    u.close()
