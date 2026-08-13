#!/usr/bin/env python3
"""Read-only sanity check to run *after* bridge.py is up in another terminal:
  1) TCP-connects to the drone's FTP control port (21) — proves the network path works.
  2) Logs in and does a passive-mode directory listing only (no STOR, nothing written).
Run from a second terminal while bridge.py keeps running in the first one; does not
need root itself. Not the flash — just proof the pipe is alive end-to-end.
"""
import socket
import sys
from ftplib import FTP

DRONE_IP = "192.168.42.2"

def tcp_check(port=21, timeout=5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((DRONE_IP, port))
        print(f"TCP connect to {DRONE_IP}:{port} — OK")
        return True
    except OSError as e:
        print(f"TCP connect to {DRONE_IP}:{port} — FAILED: {e}")
        return False
    finally:
        s.close()

def ftp_check(user, pwd):
    ftp = FTP()
    ftp.connect(DRONE_IP, 21, timeout=8)
    print("FTP banner:", ftp.getwelcome())
    ftp.login(user, pwd)
    ftp.set_pasv(True)
    print(f"FTP login as {user!r} — OK")
    files = []
    ftp.dir(files.append)
    print("Directory listing (read-only):")
    for f in files:
        print(" ", f)
    ftp.quit()

if __name__ == "__main__":
    if not tcp_check():
        sys.exit(1)
    # Credentials recovered from static analysis of the Drone-Hacks client — read-only
    # use here (login + LIST only). Pass explicitly so nothing sensitive sits in this file's history.
    if len(sys.argv) != 3:
        print("Usage: python3 verify.py <ftp_user> <ftp_pass>")
        sys.exit(0)
    ftp_check(sys.argv[1], sys.argv[2])
