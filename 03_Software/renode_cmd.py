"""Send one command to Renode's monitor and print the reply.

Usage:
    python renode_cmd.py sysbus ReadWord 0x20000228
"""
import socket, sys, time, re

cmd = " ".join(sys.argv[1:])
if not cmd:
    print("usage: python renode_cmd.py <renode command>")
    sys.exit(1)

s = socket.create_connection(("127.0.0.1", 1234), timeout=3)
s.settimeout(2.0)

time.sleep(0.4)
try:
    while True:
        chunk = s.recv(4096)
        if not chunk: break
        if b") " in chunk[-8:]: break
except socket.timeout:
    pass

s.sendall((cmd + "\n").encode("ascii"))

buf = b""
try:
    while True:
        chunk = s.recv(4096)
        if not chunk: break
        buf += chunk
        if b") " in chunk[-8:]: break
except socket.timeout:
    pass

text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", buf.decode("ascii", errors="replace"))
print(text.strip())
s.close()