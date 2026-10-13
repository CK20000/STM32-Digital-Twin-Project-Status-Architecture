import zmq
import binascii

context = zmq.Context()
subscriber = context.socket(zmq.SUB)
subscriber.connect("tcp://127.0.0.1:5555")   # Raw UART data from bridge
subscriber.connect("tcp://127.0.0.1:5556")   # Simulation results from solver
subscriber.setsockopt_string(zmq.SUBSCRIBE, "")
print("Listening on ZeroMQ ports 5555 and 5556...")

while True:
    try:
        message = subscriber.recv_string()
        if ":" not in message:
            print(f"[?] {message}")
            continue
        
        topic, payload = message.split(":", 1)
        
        if topic == "RS485":
            try:
                text = binascii.unhexlify(payload).decode('ascii')
                print(f"[RS485] TEXT: {text.strip()}")
            except:
                print(f"[RS485] HEX: {payload}")
        
        elif topic == "ESP":
            raw = binascii.unhexlify(payload)
            if len(raw) >= 20 and raw[0] == 0xAA and raw[1] == 0x55:
                adc_values = [(raw[2 + i*2] << 8) | raw[3 + i*2] for i in range(8)]
                print(f"[ESP] ADC VALUES: {adc_values}")
            else:
                print(f"[ESP] RAW: {payload}")
        
        elif topic == "SIM":
            # Simulation result - already formatted by solver
            print(f"[SIM] {payload}")
        
        else:
            print(f"[{topic}] {payload}")
    
    except KeyboardInterrupt:
        print("\nStopping subscriber...")
        break