import socket
import zmq
import time
import select

RENODE_IP = "127.0.0.1"
RS485_PORT = 12345
ESP_PORT = 12346
ZMQ_PUB_PORT = 5555

def connect(port, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((RENODE_IP, port))
        s.setblocking(False)
        print(f"[*] Connected to Renode on port {port} for '{name}'")
        return s
    except ConnectionRefusedError:
        print(f"[!] Could not connect to port {port}")
        return None

def main():
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    publisher.bind(f"tcp://*:{ZMQ_PUB_PORT}")
    print(f"[*] ZeroMQ Publisher bound to port {ZMQ_PUB_PORT}")

    rs485 = connect(RS485_PORT, "RS485")
    esp   = connect(ESP_PORT,   "ESP")
    if rs485 is None or esp is None:
        return

    print("[*] Bridge running (single-threaded poller). Ctrl+C to stop.")

    rs485_buf = bytearray()
    esp_buf   = bytearray()
    rs485_sock = rs485
    esp_sock   = esp

    try:
        while True:
            # Poll both sockets with 50 ms timeout
            r, _, _ = select.select([rs485_sock, esp_sock], [], [], 0.05)

            for sock in r:
                try:
                    chunk = sock.recv(4096)
                except (BlockingIOError, ConnectionResetError):
                    continue
                if not chunk:
                    continue

                if sock is rs485_sock:
                    rs485_buf.extend(chunk)
                    while b"\n" in rs485_buf:
                        line, rs485_buf = rs485_buf.split(b"\n", 1)
                        line = line.strip(b"\r")
                        if line:
                            publisher.send_string(f"RS485:{line.hex()}")
                            print(f"[UART -> ZMQ] RS485: {line.decode('ascii', errors='ignore')}")
                else:
                    esp_buf.extend(chunk)
                    while len(esp_buf) >= 20:
                        if esp_buf[0] == 0xAA and esp_buf[1] == 0x55:
                            packet = bytes(esp_buf[:20])
                            del esp_buf[:20]
                            publisher.send_string(f"ESP:{packet.hex()}")
                            adc = [(packet[2+i*2] << 8) | packet[3+i*2] for i in range(8)]
                            led = packet[18]
                            print(f"[UART -> ZMQ] ESP: ADC={adc} LEDs=0b{led:07b}")
                        else:
                            # Slide window
                            del esp_buf[0]
    except KeyboardInterrupt:
        print("\n[*] Bridge stopping...")
    finally:
        publisher.close()
        context.term()

if __name__ == "__main__":
    main()