#!/usr/bin/env python3
"""
Local Remote Control and Video Streaming Server for Fitness Robot.
Provides an MJPEG HTTP server for live camera stream and a WebSockets server
for offline remote control commands and real-time telemetry broadcasting.
"""

import os
import sys
import time
import json
import asyncio
import threading
import queue
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# Try to import cv2, otherwise define a placeholder or crash if needed
try:
    import cv2
except ImportError:
    cv2 = None

_cached_standby_frame = None

def _get_standby_frame():
    global _cached_standby_frame
    if _cached_standby_frame is not None:
        return _cached_standby_frame

    try:
        import numpy as np
        # 640x480 dark gray frame (#1a1a24 BGR: 36, 26, 26)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:] = (36, 26, 26)
        
        if cv2 is not None:
            # Draw decorative border
            cv2.rectangle(frame, (20, 20), (620, 460), (75, 60, 60), 2)
            # Add text "STANDBY" (blue/cyan status color BGR: 246, 130, 59)
            cv2.putText(frame, "STANDBY", (245, 210),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (246, 130, 59), 3, cv2.LINE_AA)
            # Add text "Start an exercise to activate camera feed"
            cv2.putText(frame, "Start an exercise to activate camera feed", (125, 270),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (170, 161, 161), 1, cv2.LINE_AA)
            
        ret, jpeg = cv2.imencode('.jpg', frame)
        if ret:
            _cached_standby_frame = jpeg.tobytes()
    except Exception as e:
        print(f"[HTTP] Failed to generate standby frame: {e}")
        
    return _cached_standby_frame

# Global thread-safe variables for the frame buffer
_frame_lock = threading.Lock()
_latest_frame = None

# References to the main application's coordination objects
_force_stop_event = None
_remote_mode_queue = None
_latest_battery_voltage = None

# Active local WebSocket clients
_connected_clients = set()
_ws_loop = None

# ─── 1. HTTP MJPEG Streaming Server ───

class StreamingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress request logging to avoid cluttering the console at 30fps
        pass

    def do_OPTIONS(self):
        """Handle CORS preflight requests from browsers."""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        global _latest_frame
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Age', '0')
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            # Allow browsers on the same LAN (including Android Chrome) to load
            # this stream cross-origin even when the frontend is on HTTPS.
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.end_headers()
            try:
                while True:
                    with _frame_lock:
                        if _latest_frame is None:
                            frame_bytes = _get_standby_frame()
                        elif cv2 is not None:
                            ret, jpeg = cv2.imencode('.jpg', _latest_frame)
                            frame_bytes = jpeg.tobytes() if ret else _get_standby_frame()
                        else:
                            frame_bytes = _get_standby_frame()

                    if frame_bytes is None:
                        # Fallback simple gray placeholder if frame is not ready
                        time.sleep(0.05)
                        continue

                    self.wfile.write(b'--frame\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(frame_bytes)))
                    self.end_headers()
                    self.wfile.write(frame_bytes)
                    self.wfile.write(b'\r\n')
                    time.sleep(0.033)  # Maintain roughly 30 FPS
            except Exception as e:
                # Client closed browser tab or disconnected
                pass
        else:
            self.send_error(404)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle HTTP requests in separate threads for multiple clients."""
    allow_reuse_address = True

def _run_http_server(host, port):
    try:
        server = ThreadedHTTPServer((host, port), StreamingHandler)
        print(f"[HTTP] Local MJPEG Stream Server active at http://{host if host != '0.0.0.0' else 'localhost'}:{port}/stream")
        server.serve_forever()
    except Exception as e:
        print(f"[HTTP] ERROR: Failed to start MJPEG HTTP server: {e}", file=sys.stderr)

# ─── 2. WebSockets Control Server ───

async def _ws_handler(websocket):
    global _force_stop_event, _remote_mode_queue, _connected_clients
    print(f"[WS] Local Control Client Connected: {websocket.remote_address}")
    _connected_clients.add(websocket)
    try:
        # Import websockets inside handler to handle errors gracefully
        import websockets
        # Send initial online status update to client upon connection
        ack = {"status": "connected", "time": time.time(), "type": "connection_status"}
        await websocket.send(json.dumps(ack))
        if _latest_battery_voltage is not None:
            await websocket.send(json.dumps({
                "type": "battery_voltage",
                "payload": {"voltage": _latest_battery_voltage},
                "ts": time.time()
            }))
        
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print(f"[WS] ERROR: Received invalid JSON: {message}")
                continue

            action = data.get("action")
            print(f"[WS] Command Received: {data}")

            if action == "force_stop":
                if _start_exercise_event is not None:
                    _start_exercise_event.clear()
                if _force_stop_event is not None:
                    _force_stop_event.set()
                    print("[WS] Force Stop signal set.")
                else:
                    print("[WS] ERROR: Force Stop requested, but no event handler configured.")

            elif action == "switch_mode":
                if _force_stop_event is not None:
                    _force_stop_event.clear()
                if _start_exercise_event is not None:
                    _start_exercise_event.clear()
                mode = data.get("mode")
                if mode and _remote_mode_queue is not None:
                    _remote_mode_queue.put(("switch", str(mode).upper()))
                    print(f"[WS] Mode Switch request put in queue: {mode.upper()}")
                else:
                    print(f"[WS] ERROR: Mode Switch requested to {mode}, but no queue configured.")

            elif action == "start_exercise":
                if _force_stop_event is not None:
                    _force_stop_event.clear()
                if _start_exercise_event is not None:
                    _start_exercise_event.set()
                    print("[WS] Start Exercise signal set.")
                else:
                    print("[WS] ERROR: Start Exercise requested, but no event handler configured.")

            elif action == "identify":
                uid = data.get("user_id")
                email = data.get("user_email")
                try:
                    import session_manager
                    session_manager.set_active_user(uid, email)
                except Exception as e:
                    print(f"[WS] Error setting active user: {e}")

            # Send acknowledgement back to client
            ack = {"status": "acknowledged", "action": action, "time": time.time()}
            await websocket.send(json.dumps(ack))
    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception as e:
        print(f"[WS] ERROR: Connection Error: {e}", file=sys.stderr)
    finally:
        _connected_clients.discard(websocket)
        print(f"[WS] Local Control Client Disconnected: {websocket.remote_address}")

async def _ws_main(host, port):
    import websockets
    async with websockets.serve(_ws_handler, host, port):
        print(f"[WS] Local WebSocket Control Server active at ws://{host if host != '0.0.0.0' else 'localhost'}:{port}")
        await asyncio.Future() # runs forever

def _run_ws_server(host, port):
    global _ws_loop
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _ws_loop = loop
        loop.run_until_complete(_ws_main(host, port))
    except Exception as e:
        print(f"[WS] ERROR: Failed to start WebSocket server: {e}", file=sys.stderr)

async def _send_to_all_clients(msg):
    global _connected_clients
    if _connected_clients:
        await asyncio.gather(*(client.send(msg) for client in _connected_clients), return_exceptions=True)

_start_exercise_event = None

# ─── 3. External API ───

def update_frame(frame):
    """
    Thread-safe update of the latest frame to stream.
    :param frame: OpenCV BGR frame image or None to clear.
    """
    global _latest_frame
    with _frame_lock:
        if frame is not None:
            _latest_frame = frame.copy()
        else:
            _latest_frame = None

def broadcast_telemetry(event_type, payload):
    """
    Thread-safe broadcast of telemetry to all connected WebSocket clients.
    :param event_type: event type string (e.g. rep, session_start, robot_state)
    :param payload: dict content payload
    """
    global _ws_loop, _connected_clients, _latest_battery_voltage
    if event_type == "battery_voltage" and isinstance(payload, dict) and "voltage" in payload:
        _latest_battery_voltage = payload["voltage"]
    if _ws_loop is not None and _connected_clients:
        msg = json.dumps({"type": event_type, "payload": payload, "ts": time.time()})
        asyncio.run_coroutine_threadsafe(_send_to_all_clients(msg), _ws_loop)

def start_servers(force_stop_event=None, remote_mode_queue=None, start_exercise_event=None, host="0.0.0.0", http_port=8081, ws_port=8001):
    """
    Start the local MJPEG HTTP server and WS remote control server in background threads.
    :param force_stop_event: threading.Event to toggle on force stop.
    :param remote_mode_queue: queue.Queue to receive switch mode commands.
    :param start_exercise_event: threading.Event to trigger start exercise.
    :param host: host IP address to bind to (defaults to all interfaces: 0.0.0.0).
    :param http_port: Port number for the HTTP MJPEG stream (default 8081).
    :param ws_port: Port number for the WebSocket server (default 8001).
    """
    global _force_stop_event, _remote_mode_queue, _start_exercise_event
    _force_stop_event = force_stop_event
    _remote_mode_queue = remote_mode_queue
    _start_exercise_event = start_exercise_event

    # Spin up MJPEG Stream HTTP Server
    http_thread = threading.Thread(target=_run_http_server, args=(host, http_port), daemon=True)
    http_thread.start()

    # Spin up WebSocket Control Server
    ws_thread = threading.Thread(target=_run_ws_server, args=(host, ws_port), daemon=True)
    ws_thread.start()

# ─── 4. Standalone Test Main ───

if __name__ == "__main__":
    import numpy as np

    print("Running local_stream_server.py in standalone test mode...")
    
    # Mock Event and Queue
    test_stop = threading.Event()
    test_queue = queue.Queue()

    start_servers(force_stop_event=test_stop, remote_mode_queue=test_queue)

    # Generate a dummy frame loop to test streaming
    h, w = 320, 480
    color = 0
    try:
        while True:
            # Generate a rotating colored frame as dummy video feed
            frame = np.zeros((h, w, 3), dtype=np.uint8)
            # Create a simple color pattern changing over time
            frame[:, :, 1] = int(128 + 127 * np.sin(color / 10.0)) # Green channel
            frame[:, :, 2] = int(128 + 127 * np.cos(color / 10.0)) # Red channel
            
            # Write text overlay indicating standalone test mode
            if cv2 is not None:
                cv2.putText(frame, "FitBot Local Stream Test", (20, 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                cv2.putText(frame, f"Frame Count: {color}", (20, 100),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
                cv2.putText(frame, f"Force Stop: {test_stop.is_set()}", (20, 150),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255) if test_stop.is_set() else (0, 255, 0), 1)
            
            update_frame(frame)
            
            # Check commands queue
            while not test_queue.empty():
                cmd_type, cmd_val = test_queue.get()
                print(f"[TEST] Dequeued queue command: Type={cmd_type}, Value={cmd_val}")
                if cmd_val == "STOP":
                    test_stop.set()

            color += 1
            time.sleep(0.033)
    except KeyboardInterrupt:
        print("\nStopping standalone test.")
