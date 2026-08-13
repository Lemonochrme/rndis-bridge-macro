#!/usr/bin/env python3
"""Dump the full USB descriptor tree (configs/interfaces/endpoints) of the DJI FPV drone.
Read-only: only issues standard GET_DESCRIPTOR requests, no state changes on the device."""
import usb.core
import usb.util
import sys

VID = 0x2ca3

def cls_name(cls, sub, proto):
    known = {
        (0x02, 0x02, 0x01): "CDC ACM (serial/DUML)",
        (0x02, 0x06, 0x00): "CDC-ECM (Ethernet)",
        (0x02, 0x0d, 0x00): "CDC-NCM",
        (0x0a, 0x00, 0x00): "CDC Data",
        (0xff, None, None): "Vendor-specific",
        (0xe0, 0x01, 0x03): "RNDIS (wireless-ctrlr subclass, common quirk-encoding)",
    }
    for (c, s, p), name in known.items():
        if c == cls and (s is None or s == sub) and (p is None or p == proto):
            return name
    return "?"

devs = list(usb.core.find(find_all=True, idVendor=VID))
if not devs:
    print(f"No USB device with VID=0x{VID:04x} found. Is the drone plugged in and powered on?")
    sys.exit(1)

for dev in devs:
    print("=" * 70)
    print(f"Device: VID=0x{dev.idVendor:04x} PID=0x{dev.idProduct:04x} bus={dev.bus} addr={dev.address}")
    try:
        print(f"  Manufacturer: {usb.util.get_string(dev, dev.iManufacturer)}")
        print(f"  Product:      {usb.util.get_string(dev, dev.iProduct)}")
        if dev.iSerialNumber:
            print(f"  Serial:       {usb.util.get_string(dev, dev.iSerialNumber)}")
    except Exception as e:
        print(f"  (string descriptor read failed: {e})")

    for cfg in dev:
        print(f"  Configuration {cfg.bConfigurationValue}: {cfg.bNumInterfaces} interfaces, "
              f"MaxPower={cfg.bMaxPower*2}mA, attrs=0x{cfg.bmAttributes:02x}")
        for intf in cfg:
            name = cls_name(intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol)
            print(f"    Interface {intf.bInterfaceNumber} alt={intf.bAlternateSetting}: "
                  f"class=0x{intf.bInterfaceClass:02x} sub=0x{intf.bInterfaceSubClass:02x} "
                  f"proto=0x{intf.bInterfaceProtocol:02x}  [{name}]"
                  + (f"  iInterface={usb.util.get_string(dev, intf.iInterface)}" if intf.iInterface else ""))
            for ep in intf:
                dir_s = "IN " if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
                type_map = {0: "Control", 1: "Isochronous", 2: "Bulk", 3: "Interrupt"}
                ep_type = type_map.get(ep.bmAttributes & 0x3, "?")
                print(f"      EP 0x{ep.bEndpointAddress:02x} {dir_s} {ep_type:9s} "
                      f"wMaxPacketSize={ep.wMaxPacketSize} bInterval={ep.bInterval}")
    print()
