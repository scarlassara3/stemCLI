#!/usr/bin/env python3

# Protocol reference: ../docs/usb-protocol.md
# Requires: pip install pyusb libusb-package

import argparse
import json
import sys
import time

import usb.core
import usb.util
import libusb_package

vendor_id = 0x1209
product_id = 0x572A
ep_out = 0x01
ep_in = 0x81
timeout_ms = 3000

ack = 0
connect_pkt = 2
control = 4
response = 5
file_header = 6
file_body = 7

opcodes = {
    "REBOOT": 0,
    "VERSION": 1, "INFO": 1,
    "WARNING": 2,
    "GET_RECORDING_SLOTS": 12,
    "GET_STATE_OF_CHARGE": 17,  # {"soc": "<pct>", "last-bt": "<mac>"}
}


def build_packet(ptype: int, payload: bytes = b"") -> bytes:
    length = len(payload) + 1
    frame = bytes([length & 0xFF, (length >> 8) & 0xFF, ptype]) + payload
    if len(frame) % 64 == 0:
        frame += b"\x00"  # avoids the transfer ending on a full-packet boundary
    return frame


def parse_packet(data: bytes):
    length = data[0] | (data[1] << 8)
    ptype = data[2]
    payload = bytes(data[3:3 + (length - 1)])
    return ptype, payload


def encode_control(opcode: int, obj=None) -> bytes:
    if obj is None:
        return bytes([opcode])
    return bytes([opcode]) + json.dumps(obj).encode("utf-8") + b"\x00"


def decode_control_response(payload: bytes):
    opcode = payload[0]
    body = payload[1:]
    if body.endswith(b"\x00"):
        body = body[:-1]
    if not body:
        return opcode, None
    return opcode, json.loads(body.decode("utf-8"))


def encode_file_body_chunk(chunk: bytes) -> bytes:
    return len(chunk).to_bytes(4, "little") + b"\x00" + chunk


def decode_file_body_chunk(payload: bytes) -> bytes:
    chunk_len = int.from_bytes(payload[0:4], "little")
    return payload[5:5 + chunk_len]


class StemPlayer:
    def __init__(self):
        backend = libusb_package.get_libusb1_backend()
        dev = usb.core.find(idVendor=vendor_id, idProduct=product_id, backend=backend)
        if dev is None:
            raise RuntimeError("stem player not found, connect via usb-c in normal mode")
        dev.set_configuration()
        self.dev = dev
        self._recv_buf = bytearray()

    def _write(self, data: bytes):
        self.dev.write(ep_out, data, timeout=timeout_ms)

    def _fill(self, want_at_least: int):
        while len(self._recv_buf) < want_at_least:
            self._recv_buf += bytes(self.dev.read(ep_in, 8210, timeout=timeout_ms))

    def _read(self) -> bytes:
        # A logical packet can span multiple USB bulk reads, and one whose total size lands
        # on an exact 64-byte boundary is followed by one pad byte (mirrors build_packet).
        # Buffer across reads and strip that pad byte explicitly.
        self._fill(2)
        length = self._recv_buf[0] | (self._recv_buf[1] << 8)
        total = 2 + length
        self._fill(total)
        packet = bytes(self._recv_buf[:total])
        del self._recv_buf[:total]
        if total % 64 == 0:
            self._fill(1)
            del self._recv_buf[:1]
        return packet

    def connect(self):
        self._write(build_packet(connect_pkt))
        try:
            ptype, _ = parse_packet(self._read())
            connected = ptype == ack
        except usb.core.USBTimeoutError:
            connected = False  # device may already consider itself connected
        # A throwaway VERSION call here lets the device settle right after CONNECT, avoiding
        # a timing race (stray ACK / timeout) that the real client avoids the same way.
        try:
            self.control_request("VERSION")
        except Exception:
            pass
        return connected

    def control_request(self, opcode_name: str, obj=None):
        opcode = opcodes[opcode_name]
        self._write(build_packet(control, encode_control(opcode, obj)))
        ptype, payload = parse_packet(self._read())
        if ptype != response:
            raise RuntimeError(f"expected RESPONSE, got packet type {ptype} (payload={payload!r})")
        resp_opcode, data = decode_control_response(payload)
        if resp_opcode != opcode:
            raise RuntimeError(f"response opcode {resp_opcode} does not match request opcode {opcode}")
        return data

    def upload_file(self, meta: dict, data: bytes, chunk_size: int = 8192):
        header_payload = json.dumps(meta).encode("utf-8") + b"\x00"
        self._write(build_packet(file_header, header_payload))
        ptype, payload = parse_packet(self._read())
        if ptype != ack:
            raise RuntimeError(f"expected ACK after FILE_HEADER, got packet type {ptype} ({payload!r})")
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + chunk_size]
            self._write(build_packet(file_body, encode_file_body_chunk(chunk)))
            ptype, payload = parse_packet(self._read())
            if ptype != ack:
                raise RuntimeError(f"expected ACK after FILE_BODY chunk, got packet type {ptype} ({payload!r})")
            offset += len(chunk)

    def close(self):
        usb.util.dispose_resources(self.dev)


def wait_for_reconnect(delays=(0.8, 2.7, 6.1, 12.2, 12.2, 12.2, 12.2)):
    last_err = None
    for delay in delays:
        time.sleep(delay)
        player = None
        try:
            player = StemPlayer()
            player.connect()
            info = player.control_request("VERSION")
            return player, info
        except Exception as e:
            last_err = e
            if player is not None:
                try:
                    player.close()
                except Exception:
                    pass
    raise RuntimeError(f"device did not come back, last error: {last_err}")


def cmd_info(player: StemPlayer, _args):
    print(json.dumps(player.control_request("INFO"), indent=2))


def cmd_storage(player: StemPlayer, _args):
    print(json.dumps(player.control_request("WARNING"), indent=2))


def cmd_slots(player: StemPlayer, _args):
    print(json.dumps(player.control_request("GET_RECORDING_SLOTS"), indent=2))


def cmd_battery(player: StemPlayer, _args):
    print(json.dumps(player.control_request("GET_STATE_OF_CHARGE"), indent=2))


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def cmd_firmware_push(player: StemPlayer, args):
    if "samara" in args.file.lower():
        print("warning: samara builds can hang the device, keep a known-good dfu ready", file=sys.stderr)
    with open(args.file, "rb") as f:
        data = f.read()
    print(f"uploading {args.file} ({len(data)} bytes) as dfu {args.name}")
    print("staging only, run firmware-apply to flash", file=sys.stderr)
    if not _confirm("proceed with upload", args.yes):
        print("aborted")
        return
    meta = {"size": len(data), "type": "dfu", "name": args.name}
    player.upload_file(meta, data)
    print("upload complete, run firmware-apply to flash and reboot")


def cmd_firmware_apply(player: StemPlayer, args):
    print("warning: this flashes and reboots the device", file=sys.stderr)
    if not _confirm("send the apply/reboot trigger now", args.yes):
        print("aborted")
        return
    player._write(build_packet(control, encode_control(opcodes["REBOOT"])))
    try:
        ptype, _payload = parse_packet(player._read())
        print(f"device acked, packet type {ptype}, rebooting")
    except usb.core.USBTimeoutError:
        print("no response before timeout, device may be rebooting")

    if args.no_wait:
        print("skipping reconnect, check info again in about 30s")
        return

    print("waiting for device to reboot")
    try:
        new_player, info = wait_for_reconnect()
        print("device back online")
        print(json.dumps(info, indent=2))
        new_player.close()
    except RuntimeError as e:
        print(f"warning: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Stem Player USB CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="firmware/bt/serial info").set_defaults(func=cmd_info)
    sub.add_parser("storage", help="storage size/free space").set_defaults(func=cmd_storage)
    sub.add_parser("slots", help="recording slot status").set_defaults(func=cmd_slots)
    sub.add_parser("battery", help="battery charge and last-paired bt mac").set_defaults(func=cmd_battery)

    p_fw_push = sub.add_parser("firmware-push", help="upload a dfu file to the device (stages only)")
    p_fw_push.add_argument("file", help="path to local dfu file")
    p_fw_push.add_argument("--name", default="3_stpl.dfu", help="internal filename sent in FILE_HEADER")
    p_fw_push.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    p_fw_push.set_defaults(func=cmd_firmware_push)

    p_fw_apply = sub.add_parser("firmware-apply", help="flash a staged dfu and reboot")
    p_fw_apply.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    p_fw_apply.add_argument("--no-wait", action="store_true", help="don't auto-poll for reconnect afterward")
    p_fw_apply.set_defaults(func=cmd_firmware_apply)

    args = parser.parse_args()

    player = StemPlayer()
    try:
        player.connect()
        args.func(player, args)
    finally:
        # firmware-apply intentionally reboots the device mid-command, dropping this handle.
        try:
            player.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
