"""
usb_diag.py — one-shot register probe against a running Renode instance.

Prereq: Renode is listening on 127.0.0.1:1234
        (run_core.bat, or: renode --port 1234 machine.resc)

Reads:
  RCC_AHB2ENR   0x40023834   bit 7 = OTG_FSEN  (set by HAL_PCD_MspInit)
  GPIOA_MODER   0x40020000   PA mode
  GPIOA_AFRH    0x40020024   PA8..PA15 alternate function
  GPIOD_MODER   0x40020C02   PD8..PD15 mode  (firmware drives PD12..PD15)
  GPIOE_MODER   0x40021000   PE0..PE7   mode  (firmware drives all of PE0..PE7)
  SYSCFG_MEMRMP 0x40013800   boot mode

Usage:
    python usb_diag.py
    python usb_diag.py --raw    # also dump each raw monitor reply
"""
import argparse
import re
import socket
import sys
import time

RENODE_HOST = "127.0.0.1"
RENODE_PORT = 1234

ANSI_RE     = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TELNET_CMDS = (0xFB, 0xFC, 0xFD, 0xFE)
PROMPT_RE   = re.compile(r"\(([A-Za-z0-9_\-\.]+)\)\s*$", re.MULTILINE)


def clean(raw: bytes) -> str:
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
    return ANSI_RE.sub("", out.decode("ascii", errors="replace")).replace("\r", "")


class Monitor:
    def __init__(self, host, port, debug=False):
        self.debug = debug
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.settimeout(0.25)
        self.buf = bytearray()
        self.prompt = None

        # Drain any greeting Renode sends on connect
        time.sleep(0.3)
        try:
            self.sock.recv(4096)
        except socket.timeout:
            pass
        self.buf.clear()

        # Renode does not emit its prompt until it sees a newline.
        # Send a blank line, then wait for the prompt to appear.
        self._send("")

        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
                if chunk:
                    self.buf.extend(chunk)
            except socket.timeout:
                pass

            text = clean(self.buf)
            if self.debug:
                print(f"    [handshake] buf so far: {text[-120:]!r}")

            # Preferred: a line ending with "(name) " — our strict pattern
            matches = PROMPT_RE.findall(text)
            if matches:
                self.prompt = f"({matches[-1]}) "
                idx = text.rfind(self.prompt)
                self.buf = bytearray(
                    text[idx + len(self.prompt):].encode("ascii", errors="ignore"))
                return

            # Fallback: any "(something)" at end of a line
            m = re.search(r"\(([^)\r\n]+)\)\s*$", text, re.MULTILINE)
            if m:
                self.prompt = m.group(0)  # includes the trailing spaces
                idx = text.rfind(self.prompt)
                self.buf = bytearray(
                    text[idx + len(self.prompt):].encode("ascii", errors="ignore"))
                return

        # Still nothing? Use the same default renode_reader.py uses.
        print("[!] No prompt detected. Falling back to '(plc_twin) '")
        self.prompt = "(plc_twin) "

    def _send(self, cmd):
        self.sock.sendall((cmd + "\n").encode("ascii"))

    def _read(self, timeout_s=2.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            text = clean(self.buf)
            if self.prompt:
                idx = text.rfind(self.prompt)
                if idx >= 0:
                    payload = text[:idx]
                    self.buf = bytearray(
                        text[idx + len(self.prompt):].encode("ascii", errors="ignore"))
                    return payload
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("monitor closed")
                self.buf.extend(chunk)
            except socket.timeout:
                continue
        text = clean(self.buf)
        if self.debug:
            print(f"    [read] timed out, buf = {text[-200:]!r}")
        return text

    def read_word(self, addr):
        self._send(f"sysbus ReadWord 0x{addr:08X}")
        raw = self._read()
        if self.debug:
            print(f"    [raw] ReadWord 0x{addr:08X} -> {raw.strip()!r}")
        toks = re.findall(r"0x([0-9a-fA-F]+)", raw)
        if not toks:
            raise ValueError(f"no hex token in reply: {raw!r}")
        return int(toks[-1], 16)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true", help="dump raw monitor replies")
    args = ap.parse_args()

    print(f"[*] Connecting to Renode monitor {RENODE_HOST}:{RENODE_PORT} ...")
    try:
        mon = Monitor(RENODE_HOST, RENODE_PORT, debug=args.raw)
    except Exception as e:
        print(f"[!] Connection failed: {type(e).__name__}: {e}")
        print("[!] Is Renode running? Try: run_core.bat")
        sys.exit(1)
    print(f"[*] Connected.  Prompt = {mon.prompt!r}")
    print()

    # --- 1. RCC_AHB2ENR.bit7 ---
    ahb2enr = mon.read_word(0x40023834)
    otg_fs_en = (ahb2enr >> 7) & 1
    print(f"RCC_AHB2ENR     = 0x{ahb2enr:08X}   OTG_FSEN = {otg_fs_en}")

    # --- 2. GPIOA MODER (Renode ReadWord is 16-bit; PA0..PA7 at +0x00, PA8..PA15 at +0x02) ---
    ga_lo = mon.read_word(0x40020000)
    ga_hi = mon.read_word(0x40020002)
    pa11_mode = (ga_hi >> ((11 - 8) * 2)) & 0x3
    pa12_mode = (ga_hi >> ((12 - 8) * 2)) & 0x3
    print(f"GPIOA_MODER_lo  = 0x{ga_lo:04X}                (PA0..PA7)")
    print(f"GPIOA_MODER_hi  = 0x{ga_hi:04X}                PA11_mode={pa11_mode}  PA12_mode={pa12_mode}")

    # --- 3. GPIOA AFRH ---
    afrh = mon.read_word(0x40020024)
    pa11_af = (afrh >> ((11 - 8) * 4)) & 0xF
    pa12_af = (afrh >> ((12 - 8) * 4)) & 0xF
    print(f"GPIOA_AFRH      = 0x{afrh:04X}                PA11_AF={pa11_af}  PA12_AF={pa12_af}")

    # --- 4. GPIOD MODER_hi (PD8..PD15) ---
    gd_hi = mon.read_word(0x40020C02)
    pd12_mode = (gd_hi >> ((12 - 8) * 2)) & 0x3
    pd13_mode = (gd_hi >> ((13 - 8) * 2)) & 0x3
    pd14_mode = (gd_hi >> ((14 - 8) * 2)) & 0x3
    pd15_mode = (gd_hi >> ((15 - 8) * 2)) & 0x3
    print(f"GPIOD_MODER_hi  = 0x{gd_hi:04X}                "
          f"PD12={pd12_mode} PD13={pd13_mode} PD14={pd14_mode} PD15={pd15_mode}")

    # --- 5. GPIOE MODER_lo (PE0..PE7) ---
    ge_lo = mon.read_word(0x40021000)
    print(f"GPIOE_MODER_lo  = 0x{ge_lo:04X}                (want 0x5555)")

    # --- 6. SYSCFG_MEMRMP ---
    memrmp = mon.read_word(0x40013800)
    print(f"SYSCFG_MEMRMP   = 0x{memrmp:04X}                MEM_MODE={memrmp & 0x7}")

    mon.close()
    print()
    print("=" * 66)
    print("INTERPRETATION")
    print("=" * 66)

    pa_mux_ok = (pa11_mode == 2 and pa12_mode == 2
                 and pa11_af == 10 and pa12_af == 10)
    other_gpio_ok = (ge_lo == 0x5555) or (gd_hi != 0)

    if otg_fs_en == 1:
        print("OTG_FSEN = 1  →  HAL_PCD_MspInit() ran.")
        if pa_mux_ok:
            print("PA11/PA12 = AF10  →  USB pins are correctly muxed.")
            print()
            print("→ Firmware is fine. The earlier 'MODER=0 AF=0' reading")
            print("  was a script bug (reading only the low half of MODER).")
        else:
            print(f"PA11/PA12 NOT AF10 (got {pa11_mode}/{pa11_af} and "
                  f"{pa12_mode}/{pa12_af})  →  MspInit ran but did not mux USB.")
            print()
            print("→ Check usb_device.c / usbd_conf.c HAL_PCD_MspInit() body.")
    else:
        print("OTG_FSEN = 0  →  HAL_PCD_MspInit() did NOT run.")
        if other_gpio_ok:
            print("But PE0..PE7 / PD12..PD15 ARE configured → GPIO writes persist")
            print("in Renode, so this is a genuine firmware gap, not a Renode")
            print("model issue.")
            print()
            print("→ MX_USB_DEVICE_Init() bailed before reaching HAL_PCD_MspInit().")
            print("  Next: grep firmware/ for MX_USB_DEVICE_Init / HAL_PCD_MspInit /")
            print("  USBD_LL_Init to see where it returns early.")
        else:
            print("And PE0..PE7 / PD12..PD15 are ALSO unconfigured in Renode.")
            print()
            print("→ Renode's STM32F4 GPIO model may not persist MODER/AFR writes")
            print("  at all. If so, the pin-mux check is meaningless in the twin")
            print("  and must move to post-fab hardware verification only.")
    print()


if __name__ == "__main__":
    main()