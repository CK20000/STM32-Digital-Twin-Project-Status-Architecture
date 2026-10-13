"""Block until the SPICE solver is publishing on ZMQ 5556."""
import sys
import time
import zmq

ctx = zmq.Context()
sub = ctx.socket(zmq.SUB)
sub.connect("tcp://127.0.0.1:5556")
sub.setsockopt_string(zmq.SUBSCRIBE, "SIM")
sub.setsockopt(zmq.RCVTIMEO, 1000)

print("[*] Waiting for solver on ZMQ 5556...")
deadline = time.time() + 120
while time.time() < deadline:
    try:
        msg = sub.recv_string()
        if msg.startswith("SIM:"):
            print(f"[+] Solver is live: {msg[:70]}...")
            sys.exit(0)
    except zmq.Again:
        continue

print("[!] Timed out waiting for solver (120 s)")
sys.exit(1)