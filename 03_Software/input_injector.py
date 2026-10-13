"""
input_injector.py — sends INJECT requests to renode_reader.py over ZMQ.

The reader owns the single Renode monitor connection, so the injector
does not touch Renode directly. It just publishes INJECT:<name>:<value>
on port 5558 and the reader performs the write.
"""
import sys
import time

import zmq

ZMQ_INJECT_PORT = 5558

# --- what to inject ---
TARGET_SYMBOL   = "in_v_in1"     # must exist in symbols.json
TARGET_VALUE    = 3967           # ~10 V at A_IN1
INJECT_INTERVAL = 0.1            # resend every 100 ms (firmware overwrites once/loop)


def main():
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.connect(f"tcp://127.0.0.1:{ZMQ_INJECT_PORT}")

    print(f"[*] Target : {TARGET_SYMBOL}")
    print(f"[*] Value  : {TARGET_VALUE}")
    print(f"[*] Rate   : {1.0/INJECT_INTERVAL:.1f} Hz")
    print(f"[*] Publishing to reader on ZMQ {ZMQ_INJECT_PORT}")
    print(f"[*] Press Ctrl+C to stop.\n")

    # ZMQ PUB/SUB has a slow-join problem — first messages can be dropped.
    # Send a few warm-up frames, then start for real.
    for _ in range(3):
        pub.send_string(f"INJECT:{TARGET_SYMBOL}:{TARGET_VALUE}")
        time.sleep(0.2)

    count = 0
    last = time.time()
    try:
        while True:
            pub.send_string(f"INJECT:{TARGET_SYMBOL}:{TARGET_VALUE}")
            count += 1
            if count % 20 == 0:
                now = time.time()
                rate = 20 / (now - last)
                last = now
                print(f"[inject] {count:>6} writes  {rate:>5.1f} Hz  "
                      f"{TARGET_SYMBOL}={TARGET_VALUE}")
            time.sleep(INJECT_INTERVAL)
    except KeyboardInterrupt:
        print("\n[!] Stopped. Zeroing input...")
        # Send zero a few times to make sure the reader picks it up.
        for _ in range(5):
            pub.send_string(f"INJECT:{TARGET_SYMBOL}:0")
            time.sleep(0.1)
        print(f"[inject] {TARGET_SYMBOL}=0 (final)")
    finally:
        pub.close()
        ctx.term()


if __name__ == "__main__":
    main()