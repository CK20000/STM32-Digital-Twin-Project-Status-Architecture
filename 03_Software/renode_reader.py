"""
Renode memory reader + writer (merged).

Owns the single Renode monitor connection (port 1234 — it only allows
one client at a time), so the injector cannot open its own.

- Reads all observables every POLL_INTERVAL_S -> publishes OBS:<name>:<hex> on 5557
- Subscribes to INJECT:<name>:<value> on 5558 -> writes via the same socket
"""
import json
import re
import socket
import struct
import sys
import time

import zmq

RENODE_HOST         = "127.0.0.1"
RENODE_MONITOR_PORT = 1234
ZMQ_OBS_PORT        = 5557
ZMQ_INJECT_PORT     = 5558
SYMBOLS_FILE        = "symbols.json"
POLL_INTERVAL_S     = 0.1
RECV_TIMEOUT_S      = 2.0

ANSI_RE     = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TELNET_CMDS = (0xFB, 0xFC, 0xFD, 0xFE)


def clean_monitor_bytes(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0xFF and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in TELNET_CMDS and i + 2 < len(raw):
                i += 3
                continue
            if nxt == 0xFF:
                out.append(0xFF)
                i += 2
                continue
            i += 2
            continue
        out.append(b)
        i += 1
    text = ANSI_RE.sub("", out.decode("ascii", errors="replace"))
    return text.replace("\r", "")


class RenodeMonitor:
    def __init__(self, host, port, machine_name="plc_twin"):
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.settimeout(0.25)
        self.buf_raw = bytearray()
        self.prompt = f"({machine_name}) "
        time.sleep(0.3)
        try:
            self.sock.recv(4096)
        except socket.timeout:
            pass
        self.buf_raw.clear()
        self._send("")
        self._read_until_prompt(timeout_s=5.0, debug=True)

    def _send(self, cmd):
        self.sock.sendall((cmd + "\n").encode("ascii"))

    def _read_until_prompt(self, timeout_s=None, debug=False):
        if timeout_s is None:
            timeout_s = RECV_TIMEOUT_S
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            text = clean_monitor_bytes(self.buf_raw)
            idx = text.rfind(self.prompt)
            if idx >= 0:
                payload = text[:idx]
                tail = text[idx + len(self.prompt):]
                self.buf_raw = bytearray(tail.encode("ascii", errors="ignore"))
                return payload
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Monitor closed")
                self.buf_raw.extend(chunk)
            except socket.timeout:
                continue
        if debug:
            print(f"[dbg] Timeout. Text: {clean_monitor_bytes(self.buf_raw)[-200:]!r}")
        raise TimeoutError(f"No '{self.prompt}' within {timeout_s}s")

    def read_bytes(self, address, count):
        self._send(f"sysbus ReadBytes 0x{address:08X} {count}")
        raw = self._read_until_prompt()
        tokens = re.findall(r"0x([0-9a-fA-F]{1,2})\b", raw)
        values = [int(t, 16) for t in tokens]
        if len(values) == count + 1:
            values = values[1:]
        if len(values) < count:
            raise ValueError(f"Wanted {count} bytes, got {len(values)}: {raw!r}")
        return bytes(values[:count])


    def read_word(self, address):
        """16-bit word read (Renode gpioPortA/SYSCFG don't support byte reads)."""
        self._send(f"sysbus ReadWord 0x{address:08X}")
        raw = self._read_until_prompt()
        toks = re.findall(r"0x([0-9a-fA-F]+)", raw)
        if not toks:
            raise ValueError(f"No value in response: {raw!r}")
        return int(toks[-1], 16)


    def write_word(self, address, value):
        """Write 16-bit value at address. value is truncated to 16 bits."""
        self._send(f"sysbus WriteWord 0x{address:08X} {int(value) & 0xFFFF}")
        self._read_until_prompt()    # drain the response / prompt

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

# USB-relevant Cortex-M4 registers. Each entry is documented with the
# access width the new snapshot_usb_regs() uses. SYSCFG_MEMRMP is NOT
# here because Renode's STM32F4 model has no SYSCFG peripheral; reading
# it returns nothing usable.
USB_REGS = {
    "GPIOA_MODER":   0x40020000,   # 32-bit, PA mode
    "GPIOA_AFRH":    0x40020024,   # 32-bit, PA8..PA15 AF
    "RCC_AHB2ENR":   0x40023834,   # 16-bit, bit 7 = OTG_FSEN
}


def snapshot_usb_regs(mon):
    """Snapshot of USB-relevant registers via 16-bit word reads.

    gpioPortA and rcc in Renode's STM32F4 model reject byte reads — a
    byte read returns a stub. All reads here are ReadWord (16-bit).
    """
    out = {}

    def dword(addr):
        lo = mon.read_word(addr)
        hi = mon.read_word(addr + 2)
        return (hi << 16) | lo

    try:
        out["GPIOA_MODER"] = dword(0x40020000)      # PA0..PA15 mode
    except Exception as e:
        print(f"[!] GPIOA_MODER: {type(e).__name__}: {e}")

    try:
        out["GPIOA_AFRH"] = dword(0x40020024)       # PA8..PA15 AF
    except Exception as e:
        print(f"[!] GPIOA_AFRH: {type(e).__name__}: {e}")

    try:
        out["RCC_AHB2ENR"] = mon.read_word(0x40023834)   # bit 7 = OTG_FSEN
    except Exception as e:
        print(f"[!] RCC_AHB2ENR: {type(e).__name__}: {e}")

    try:
        out["SYSCFG_MEMRMP"] = mon.read_word(0x40013800)  # word read works
    except Exception as e:
        print(f"[!] SYSCFG_MEMRMP: {type(e).__name__}: {e}")

    return out


def main():
    with open(SYMBOLS_FILE) as f:
        data = json.load(f)
    obs_list = data.get("observables", [])
    if not obs_list:
        print("[!] No observables in symbols.json. Run get_symbols.py.")
        sys.exit(1)

    entries = []
    for o in obs_list:
        entries.append({
            "name": o["name"],
            "addr": int(o["addr"], 16),
            "size": o["size"],
        })
    name_to_addr = {e["name"]: e["addr"] for e in entries}

    min_addr = min(e["addr"] for e in entries)
    max_end  = max(e["addr"] + e["size"] for e in entries)
    span     = max_end - min_addr

    print(f"[*] Discovered {len(entries)} observables")
    print(f"[*] Contiguous span: 0x{min_addr:08X} .. 0x{max_end:08X}  ({span} bytes)")

    print(f"[*] Connecting to Renode monitor {RENODE_HOST}:{RENODE_MONITOR_PORT}...")
    try:
        mon = RenodeMonitor(RENODE_HOST, RENODE_MONITOR_PORT)
    except Exception as e:
        print(f"[!] Connection failed: {type(e).__name__}: {e}")
        sys.exit(1)
    print("[*] Connected.")
    # One-shot USB register snapshot — published once on the OBS port.
    usb_regs = snapshot_usb_regs(mon)
    if usb_regs:
        print(f"[*] USB registers: " +
              ", ".join(f"{k}=0x{v:08X}" for k, v in usb_regs.items()))
    try:
        test = mon.read_bytes(min_addr, span)
        print(f"[*] Test read span ({span} bytes): {test.hex()}")
    except Exception as e:
        print(f"[!] Test read failed: {type(e).__name__}: {e}")
        sys.exit(1)

    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://*:{ZMQ_OBS_PORT}")

    sub = ctx.socket(zmq.SUB)
    sub.bind(f"tcp://*:{ZMQ_INJECT_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "INJECT")

    print(f"[*] Publishing OBS on {ZMQ_OBS_PORT}")
    # USBREG is republished every read cycle (see the loop below), so
    # late-joining subscribers always get the values.
    print(f"[*] Listening  INJECT on {ZMQ_INJECT_PORT}")
    print(f"[*] Batched read — 1 monitor cmd/cycle, injections interleaved")

    packets = 0
    last_report = time.time()
    last_written = {}  # name -> last value written, to skip no-ops

    try:
        while True:
            # --- Coalesce all pending injection requests ---
            # Keep only the LATEST value per symbol, so 1000 rapid slider
            # events collapse into at most one write per channel per cycle.
            pending = {}
            while True:
                try:
                    msg = sub.recv_string(zmq.NOBLOCK)
                except zmq.Again:
                    break
                try:
                    _, name, value_str = msg.split(":", 2)
                    pending[name] = int(value_str)
                except Exception as e:
                    print(f"[!] INJECT parse error: {e}")

            # Apply at most once per symbol, and only if value changed
            for name, value in pending.items():
                addr = name_to_addr.get(name)
                if addr is None:
                    print(f"[!] INJECT unknown symbol: {name}")
                    continue
                if last_written.get(name) == value:
                    continue
                mon.write_word(addr, value)
                last_written[name] = value
                print(f"[inject] {name} @ 0x{addr:08X} = {value}")

            # --- Normal read cycle ---
            t0 = time.time()
            try:
                blob = mon.read_bytes(min_addr, span)
                for e in entries:
                    off = e["addr"] - min_addr
                    chunk = blob[off:off + e["size"]]
                    pub.send_string(f"OBS:{e['name']}:{chunk.hex()}")
                for name, val in usb_regs.items():
                    pub.send_string(f"USBREG:{name}:{val:08x}")
                packets += 1
            except (TimeoutError, ValueError, ConnectionError) as e:
                print(f"[!] Read error: {type(e).__name__}: {e}")
                try:
                    mon.close()
                    time.sleep(0.5)
                    mon = RenodeMonitor(RENODE_HOST, RENODE_MONITOR_PORT)
                    print("[*] Reconnected.")
                except Exception as e2:
                    print(f"[!] Reconnect failed: {e2}")
                    time.sleep(1.0)

            elapsed = time.time() - t0
            if elapsed < POLL_INTERVAL_S:
                time.sleep(POLL_INTERVAL_S - elapsed)

            if packets % 50 == 0 and packets > 0:
                now = time.time()
                rate = 50 / (now - last_report)
                last_report = now
                print(f"[reader] {packets:>7} pkts  {rate:>5.1f} Hz  ({span} bytes/cycle)")

    except KeyboardInterrupt:
        print("\n[*] Stopping...")
    finally:
        mon.close()
        pub.close()
        sub.close()
        ctx.term()


if __name__ == "__main__":
    main()