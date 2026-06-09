import serial
import time
import sys
import threading
import argparse

# Default UART configurations on Raspberry Pi:
# 1. Primary UART: '/dev/serial0' -> GPIO 14 (TX) & GPIO 15 (RX). Built-in serial port.
# 2. UART 5: '/dev/ttyAMA1' -> GPIO 12 (TX) & GPIO 13 (RX). Requires 'dtoverlay=uart5' in /boot/firmware/config.txt

def read_from_serial(ser, stop_event):
    """Continuously read from serial in a background thread."""
    while not stop_event.is_set():
        try:
            if ser.in_waiting:
                incoming = ser.readline().decode('utf-8', errors='ignore').strip()
                if incoming:
                    print(f"\n📩 Incoming from ESP32: {incoming}")
                    # Reprint the prompt to keep terminal clean
                    print("Send (or type 'exit' / 'ping'): ", end="", flush=True)
            else:
                time.sleep(0.05)
        except Exception as e:
            print(f"\n❌ Read error: {e}")
            break

def main():
    parser = argparse.ArgumentParser(description="Raspberry Pi <-> ESP32 Serial Test Utility")
    parser.add_argument(
        "--port", 
        type=str, 
        default="/dev/serial0",
        help="Serial port to use (default: /dev/serial0). E.g., /dev/ttyAMA1 for UART5."
    )
    parser.add_argument(
        "--baud", 
        type=int, 
        default=115200, 
        help="Baud rate (default: 115200)"
    )
    args = parser.parse_args()

    print("========================================")
    print("  Raspberry Pi <-> ESP32 Serial Test")
    print("========================================")
    print(f"Connecting to port: {args.port}")
    print(f"Baud rate:         {args.baud}")
    print("----------------------------------------")
    print("📌 Wiring Guide:")
    print("   - Pi TX pin goes to ESP32 RX pin")
    print("   - Pi RX pin goes to ESP32 TX pin")
    print("   - Pi GND must connect to ESP32 GND")
    print("========================================\n")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
        time.sleep(2)  # Allow port connection to stabilize
        print(f"✅ Successfully opened {args.port} at {args.baud} baud.\n")
    except Exception as e:
        print(f"❌ Failed to open serial port {args.port}: {e}")
        print("\n💡 Tips:")
        print("  - Run with 'sudo' if you get permission errors: sudo python serial_test_pi.py")
        print("  - If using /dev/serial0, ensure Serial Port is enabled in raspi-config (and Serial Console is disabled)")
        print("  - If using /dev/ttyAMA1 (UART5), ensure 'dtoverlay=uart5' is in /boot/firmware/config.txt")
        sys.exit(1)

    # Start background read thread
    stop_event = threading.Event()
    read_thread = threading.Thread(target=read_from_serial, args=(ser, stop_event), daemon=True)
    read_thread.start()

    print("--- Interactive Mode ---")
    print("• Type message and press Enter to send.")
    print("• Type 'ping' to send a PING command.")
    print("• Type 'exit' or 'quit' to close.")
    print("------------------------\n")

    try:
        while True:
            # Get user input
            msg = input("Send (or type 'exit' / 'ping'): ").strip()
            
            if msg.lower() in ['exit', 'quit']:
                break
            
            if msg.lower() == 'ping':
                msg = "PING"
            
            # Send command
            try:
                ser.write((msg + "\n").encode('utf-8'))
                ser.flush()
                print(f"📤 Sent: {msg}")
            except Exception as e:
                print(f"❌ Send error: {e}")
                break
                
            # Small delay to let background thread capture print output before next loop prompt
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nTest stopped by user.")
    finally:
        stop_event.set()
        ser.close()
        print("\nSerial port closed.")

if __name__ == "__main__":
    main()

