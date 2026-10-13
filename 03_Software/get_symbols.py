"""
Discover every firmware observable and extract its RAM address + size.

A firmware observable is any global symbol whose name starts with
'obs_'. Conventionally it should mirror the SPICE source name it drives:
    SPICE:  V_in1, V_in7, V_daq1, V_D4_drv
    Firmware:  obs_v_in1, obs_v_in7, obs_v_daq1, obs_v_d4_drv

The size is picked up from the linker .map file (single-line or
two-line section formats are both handled).
"""
import json
import os
import re
import sys

MAP_CANDIDATES = [
    "firmware/automation_plc.map",
    "firmware/Debug/automation_plc.map",
    "../firmware/Debug/automation_plc.map",
    "Debug/automation_plc.map",
]
OUTPUT = "symbols.json"
PREFIXES = ("obs_", "in_")


def find_map_file():
    for c in MAP_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def parse_map(path):
    """Return {name: (addr, size)} for every symbol in the map.

    Handles the two common patterns emitted by GNU ld:
      A) single-line:  .bss.obs_v_in1    0x20000228   0x2 main.o
      B) two-line:     .bss.obs_v_in1
                       0x0000000020000228   0x2 main.o
    Also picks up symbols from the 'Global Symbols' table when present.
    """
    syms = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # Pattern A — single-line section + address + size
    for m in re.finditer(
        r"^\s*\.\w+\.(\w+)\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)",
        text, re.MULTILINE):
        name = m.group(1)
        syms[name] = (int(m.group(2), 16), int(m.group(3), 16))

    # Pattern B — section name on its own line, then addr + size on next
    for m in re.finditer(
        r"^\s*\.\w+\.(\w+)\s*\n\s*0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)",
        text, re.MULTILINE):
        name = m.group(1)
        if name not in syms:
            syms[name] = (int(m.group(2), 16), int(m.group(3), 16))

    # Fallback — bare "name 0xADDR" from the Global Symbols table
    for m in re.finditer(
        r"^\s*(\w+)\s+0x([0-9a-fA-F]{8,})(?:\s+0x([0-9a-fA-F]+))?",
        text, re.MULTILINE):
        name = m.group(1)
        if name in syms:
            continue
        try:
            addr = int(m.group(2), 16)
            size = int(m.group(3), 16) if m.group(3) else 0
            syms[name] = (addr, size)
        except (ValueError, AttributeError):
            continue

    return syms


def main():
    path = find_map_file()
    if not path:
        print("[!] Could not find automation_plc.map")
        for c in MAP_CANDIDATES:
            print(f"      {c}")
        sys.exit(1)

    print(f"[*] Parsing {path}")
    syms = parse_map(path)
    print(f"[*] Found {len(syms)} symbols total")

    observables = []
    for name, (addr, size) in sorted(syms.items()):
        if not name.startswith(PREFIXES):
            continue
        if size == 0:
            print(f"  [warn] {name}: no size in map, skipping")
            continue
        observables.append({
            "name": name,
            "addr": f"0x{addr:08X}",
            "size": size,
        })

    if not observables:
        print(f"[!] No {' / '.join(PREFIXES)}* symbols found.")
        print(f"    Firmware observables must be named 'obs_<source>' or")
        print(f"    'in_<source>' and declared as volatile with __attribute__((used)).")
        sys.exit(1)

    print(f"\n[*] Discovered {len(observables)} observables:")
    for o in observables:
        print(f"      {o['name']:32s}  {o['addr']}  {o['size']:>3} bytes")

    with open(OUTPUT, "w") as f:
        json.dump({"observables": observables}, f, indent=2)
    print(f"\n[+] Wrote {OUTPUT}")


if __name__ == "__main__":
    main()