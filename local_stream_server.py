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
from pathlib import Path
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
_start_exercise_event = None
_pause_request_event = None
_resume_request_event = None
_latest_battery_voltage = None
_latest_robot_state = None

# Active local WebSocket clients & user account locks
_connected_clients = set()
_ws_loop = None
_active_user_id = None
_active_user_email = None
_client_accounts = {}  # websocket -> {"user_id": uid, "user_email": email}
_known_accounts = {}   # uid or email -> account metadata

ACCOUNTS_CACHE_FILE = Path(__file__).parent / "known_accounts.json"

def _load_known_accounts():
    global _known_accounts
    try:
        if ACCOUNTS_CACHE_FILE.exists():
            with open(ACCOUNTS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _known_accounts = data
                    # Mark all accounts as offline upon server startup
                    for k, item in _known_accounts.items():
                        item["is_online"] = False
                        item["connected_to_robot"] = False
            print(f"[WS] Loaded {len(_known_accounts)} known accounts from {ACCOUNTS_CACHE_FILE.name}")
    except Exception as e:
        print(f"[WS] Warning: Failed to load known accounts cache: {e}")

def _save_known_accounts():
    try:
        with open(ACCOUNTS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_known_accounts, f, indent=2)
    except Exception as e:
        print(f"[WS] Warning: Failed to save known accounts cache: {e}")

_load_known_accounts()

def _build_status_report():
    """Build comprehensive real-time status report including lock state and all active accounts."""
    now_ts = time.time()
    online_keys = set()
    for acc in _client_accounts.values():
        uid = acc.get("user_id")
        email = acc.get("user_email")
        if uid or email:
            key = str(uid or email)
            online_keys.add(key)
            _known_accounts[key] = {
                "uid": uid or key,
                "email": email or key,
                "is_online": True,
                "connected_to_robot": bool((uid and uid == _active_user_id) or (email and email == _active_user_email)),
                "last_seen": now_ts
            }

    # Mark disconnected accounts offline
    for k, item in _known_accounts.items():
        if k not in online_keys:
            item["is_online"] = False
            item["connected_to_robot"] = False

    _save_known_accounts()

    accounts_list = list(_known_accounts.values())
    connected_list = [acc for acc in accounts_list if acc.get("is_online")]

    try:
        import audio_manager
        is_muted = audio_manager.is_feedback_muted()
    except Exception:
        is_muted = False

    return {
        "type": "robot_status_report",
        "is_locked": _active_user_id is not None,
        "locked_user_email": _active_user_email,
        "locked_user_id": _active_user_id,
        "battery_voltage": _latest_battery_voltage,
        "robot_state": _latest_robot_state,
        "is_muted": is_muted,
        "connected_clients_count": len(_connected_clients),
        "connected_accounts": connected_list,
        "all_accounts": accounts_list,
        "time": now_ts
    }

async def _broadcast_status():
    """Broadcast latest status report to all connected clients."""
    if _connected_clients:
        msg = json.dumps(_build_status_report())
        await _send_to_all_clients(msg)

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
        elif self.path in ('/api/control', '/control', '/switch_mode'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"status": "ready"}')
        else:
            self.send_error(404)

    def do_POST(self):
        global _force_stop_event, _remote_mode_queue, _start_exercise_event, _pause_request_event, _resume_request_event
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else b'{}'
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            data = {}

        action = str(data.get("action") or "").lower()
        mode = data.get("mode") or data.get("exercise")

        if action == "force_stop":
            if _start_exercise_event: _start_exercise_event.clear()
            if _force_stop_event: _force_stop_event.set()
            # Drain remote_mode_queue so the exercise doesn't restart repeatedly
            if _remote_mode_queue:
                while not _remote_mode_queue.empty():
                    try:
                        _remote_mode_queue.get_nowait()
                    except Exception:
                        break
        elif action == "pause":
            if _pause_request_event: _pause_request_event.set()
            if _resume_request_event: _resume_request_event.clear()
        elif action == "resume":
            if _resume_request_event: _resume_request_event.set()
            if _pause_request_event: _pause_request_event.clear()
        elif action in ("mute", "unmute", "set_mute"):
            muted = data.get("muted") if "muted" in data else (action == "mute")
            try:
                import audio_manager
                audio_manager.set_feedback_muted(muted)
                broadcast_telemetry("mute_state", {"muted": muted})
            except Exception as e:
                print(f"[HTTP] Error setting mute: {e}")
        elif action in ("switch_mode", "start_exercise", "start", "select_exercise"):
            if _force_stop_event: _force_stop_event.clear()
            if _start_exercise_event: _start_exercise_event.set()
            if mode and _remote_mode_queue:
                while not _remote_mode_queue.empty():
                    try:
                        _remote_mode_queue.get_nowait()
                    except Exception:
                        break
                _remote_mode_queue.put(("switch", str(mode).upper()))
        elif str(action).upper() in ("BICEP_CURLS", "PUSHUPS", "SQUATS", "PLANK"):
            if _force_stop_event: _force_stop_event.clear()
            if _start_exercise_event: _start_exercise_event.set()
            if _remote_mode_queue:
                while not _remote_mode_queue.empty():
                    try:
                        _remote_mode_queue.get_nowait()
                    except Exception:
                        break
                _remote_mode_queue.put(("switch", str(action).upper()))

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "action": action, "mode": mode}).encode('utf-8'))

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
    global _force_stop_event, _remote_mode_queue, _connected_clients, _active_user_id, _active_user_email, _client_accounts
    print(f"[WS] Local Control Client Connected: {websocket.remote_address}")
    _connected_clients.add(websocket)
    try:
        # Import websockets inside handler to handle errors gracefully
        import websockets
        # Send initial complete status report upon connection
        init_report = _build_status_report()
        init_report["type"] = "connection_status"
        init_report["status"] = "connected"
        await websocket.send(json.dumps(init_report))
        if _latest_battery_voltage is not None:
            await websocket.send(json.dumps({
                "type": "battery_voltage",
                "payload": {"voltage": _latest_battery_voltage},
                "ts": time.time()
            }))
        if _latest_robot_state is not None:
            await websocket.send(json.dumps({
                "type": "robot_state",
                "payload": _latest_robot_state,
                "ts": time.time()
            }))
        try:
            import audio_manager
            await websocket.send(json.dumps({
                "type": "mute_state",
                "payload": {"muted": audio_manager.is_feedback_muted()},
                "ts": time.time()
            }))
        except Exception:
            pass
        
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print(f"[WS] ERROR: Received invalid JSON: {message}")
                continue

            action = str(data.get("action") or "").lower()
            print(f"[WS] Command Received: {data}")

            if action == "identify":
                uid = data.get("user_id")
                email = data.get("user_email")

                # If the robot is already connected to another account, reject this connection!
                if _active_user_id is not None and uid and uid != _active_user_id:
                    print(f"[WS] ⚠️ REJECTED: Account '{email}' ({uid}) attempted to connect, but robot is already connected to '{_active_user_email}' ({_active_user_id}).")
                    err_resp = {
                        "type": "connection_rejected",
                        "status": "rejected",
                        "error": "account_locked",
                        "message": f"Robot is currently connected to another account ({_active_user_email}). Only one account can connect at a time.",
                        "locked_user_email": _active_user_email
                    }
                    await websocket.send(json.dumps(err_resp))
                    await websocket.close(code=4003, reason="Robot locked by another account")
                    return

                # Lock to this account
                _active_user_id = uid
                _active_user_email = email
                _client_accounts[websocket] = {"user_id": uid, "user_email": email}
                try:
                    import session_manager
                    session_manager.set_active_user(uid, email)
                except Exception as e:
                    print(f"[WS] Error setting active user: {e}")

                ack = {
                    "status": "acknowledged",
                    "action": action,
                    "type": "connection_status",
                    "connected": True,
                    "is_locked": True,
                    "locked_user_id": uid,
                    "locked_user_email": email,
                    "user_id": uid,
                    "user_email": email,
                    "time": time.time()
                }
                await websocket.send(json.dumps(ack))
                await _broadcast_status()
                continue

            elif action == "disconnect":
                uid = data.get("user_id")
                if _active_user_id == uid or not uid:
                    print(f"[WS] Disconnect requested by active user: {_active_user_email}")
                    _active_user_id = None
                    _active_user_email = None
                    try:
                        import session_manager
                        session_manager.clear_active_user()
                    except Exception as e:
                        print(f"[WS] Error clearing active user: {e}")
                _client_accounts.pop(websocket, None)
                ack = {
                    "status": "acknowledged",
                    "action": "disconnect",
                    "type": "connection_status",
                    "connected": False,
                    "is_locked": False,
                    "time": time.time()
                }
                await websocket.send(json.dumps(ack))
                await _broadcast_status()
                await websocket.close()
                return

            elif action == "admin_force_disconnect":
                admin_email = data.get("admin_email", "admin@sydts.ai")
                print(f"[WS] ⚠️ ADMIN FORCE DISCONNECT requested by {admin_email}. Kicking user {_active_user_email} ({_active_user_id}).")
                prev_user_email = _active_user_email
                prev_user_id = _active_user_id

                # Broadcast force_disconnected to active client sockets
                kick_msg = json.dumps({
                    "type": "force_disconnected",
                    "reason": f"Your robot connection was disconnected by administrator ({admin_email}).",
                    "time": time.time()
                })
                for sock, acc in list(_client_accounts.items()):
                    if acc.get("user_id") == prev_user_id:
                        try:
                            asyncio.create_task(sock.send(kick_msg))
                            asyncio.create_task(sock.close(code=4001, reason="Disconnected by administrator"))
                        except Exception:
                            pass

                _active_user_id = None
                _active_user_email = None
                try:
                    import session_manager
                    session_manager.clear_active_user()
                except Exception as e:
                    print(f"[WS] Error clearing active user during admin disconnect: {e}")

                ack = {
                    "status": "acknowledged",
                    "action": "admin_force_disconnect",
                    "type": "connection_status",
                    "disconnected_user": prev_user_email,
                    "is_locked": False,
                    "time": time.time()
                }
                await websocket.send(json.dumps(ack))
                await _broadcast_status()
                continue

            elif action == "admin_delete_account":
                target_uid = data.get("user_id")
                target_email = data.get("user_email")
                print(f"[WS] ⚠️ ADMIN DELETE ACCOUNT: target={target_email} ({target_uid})")
                for k in list(_known_accounts.keys()):
                    if (target_uid and k == target_uid) or (target_email and k == target_email):
                        _known_accounts.pop(k, None)
                _save_known_accounts()
                for sock, acc in list(_client_accounts.items()):
                    if (target_uid and acc.get("user_id") == target_uid) or (target_email and acc.get("user_email") == target_email):
                        try:
                            kick_msg = json.dumps({
                                "type": "force_disconnected",
                                "reason": "Your account was deleted by administrator.",
                                "time": time.time()
                            })
                            asyncio.create_task(sock.send(kick_msg))
                            asyncio.create_task(sock.close(code=4001, reason="Account deleted by administrator"))
                        except Exception:
                            pass
                        _client_accounts.pop(sock, None)
                if (target_uid and _active_user_id == target_uid) or (target_email and _active_user_email == target_email):
                    _active_user_id = None
                    _active_user_email = None
                    try:
                        import session_manager
                        session_manager.clear_active_user()
                    except Exception:
                        pass
                ack = {
                    "status": "acknowledged",
                    "action": "admin_delete_account",
                    "deleted_user": target_email or target_uid,
                    "time": time.time()
                }
                await websocket.send(json.dumps(ack))
                await _broadcast_status()
                continue

            elif action in ("get_status", "get_accounts"):
                await websocket.send(json.dumps(_build_status_report()))
                continue

            if action == "force_stop":
                if _start_exercise_event is not None:
                    _start_exercise_event.clear()
                if _force_stop_event is not None:
                    _force_stop_event.set()
                    print("[WS] Force Stop signal set.")
                else:
                    print("[WS] ERROR: Force Stop requested, but no event handler configured.")
                # Drain the mode queue so no queued switches re-launch after stopping
                if _remote_mode_queue is not None:
                    while not _remote_mode_queue.empty():
                        try:
                            _remote_mode_queue.get_nowait()
                        except Exception:
                            break

            elif action == "pause":
                if _pause_request_event is not None:
                    _pause_request_event.set()
                if _resume_request_event is not None:
                    _resume_request_event.clear()
                print("[WS] Pause signal set.")

            elif action == "resume":
                if _resume_request_event is not None:
                    _resume_request_event.set()
                if _pause_request_event is not None:
                    _pause_request_event.clear()
                print("[WS] Resume signal set.")

            elif action in ("mute", "unmute", "set_mute"):
                muted = data.get("muted") if "muted" in data else (action == "mute")
                try:
                    import audio_manager
                    audio_manager.set_feedback_muted(muted)
                    broadcast_telemetry("mute_state", {"muted": muted})
                    print(f"[WS] Feedback mute state set to: {muted}")
                except Exception as e:
                    print(f"[WS] Error setting mute: {e}")

            elif action in ("switch_mode", "select_exercise"):
                if _force_stop_event is not None:
                    _force_stop_event.clear()
                if _start_exercise_event is not None:
                    _start_exercise_event.set()
                mode = data.get("mode") or data.get("exercise")
                if mode and _remote_mode_queue is not None:
                    while not _remote_mode_queue.empty():
                        try:
                            _remote_mode_queue.get_nowait()
                        except Exception:
                            break
                    _remote_mode_queue.put(("switch", str(mode).upper()))
                    print(f"[WS] Mode Switch request put in queue: {mode.upper()}")
                else:
                    print(f"[WS] ERROR: Mode Switch requested to {mode}, but no queue configured.")

            elif action in ("start_exercise", "start"):
                if _force_stop_event is not None:
                    _force_stop_event.clear()
                if _start_exercise_event is not None:
                    _start_exercise_event.set()
                    print("[WS] Start Exercise signal set.")
                mode = data.get("exercise") or data.get("mode")
                if mode and _remote_mode_queue is not None:
                    while not _remote_mode_queue.empty():
                        try:
                            _remote_mode_queue.get_nowait()
                        except Exception:
                            break
                    _remote_mode_queue.put(("switch", str(mode).upper()))
                    print(f"[WS] Start Exercise mode switch put in queue: {mode.upper()}")

            elif str(action).upper() in ("BICEP_CURLS", "BICEP_CURL", "PUSHUPS", "PUSH_UPS", "SQUATS", "SQUAT", "PLANK"):
                if _force_stop_event is not None:
                    _force_stop_event.clear()
                if _start_exercise_event is not None:
                    _start_exercise_event.set()
                if _remote_mode_queue is not None:
                    while not _remote_mode_queue.empty():
                        try:
                            _remote_mode_queue.get_nowait()
                        except Exception:
                            break
                    _remote_mode_queue.put(("switch", str(action).upper()))
                    print(f"[WS] Direct exercise action put in queue: {str(action).upper()}")

            # Send acknowledgement back to client
            ack = {"status": "acknowledged", "action": action, "time": time.time()}
            await websocket.send(json.dumps(ack))
    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception as e:
        print(f"[WS] ERROR: Connection Error: {e}", file=sys.stderr)
    finally:
        _connected_clients.discard(websocket)
        disconnected_info = _client_accounts.pop(websocket, None)
        if disconnected_info and disconnected_info.get("user_id") == _active_user_id:
            # Check if any other socket from the same user is still open
            still_active = any(acc.get("user_id") == _active_user_id for acc in _client_accounts.values())
            if not still_active:
                print(f"[WS] All connections for active user {_active_user_email} closed. Releasing robot lock.")
                _active_user_id = None
                _active_user_email = None
                try:
                    import session_manager
                    session_manager.clear_active_user()
                except Exception:
                    pass
        print(f"[WS] Local Control Client Disconnected: {websocket.remote_address}")
        try:
            await _broadcast_status()
        except Exception:
            pass

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
    global _ws_loop, _connected_clients, _latest_battery_voltage, _latest_robot_state
    if event_type == "battery_voltage" and isinstance(payload, dict) and "voltage" in payload:
        _latest_battery_voltage = payload["voltage"]
    elif event_type == "robot_state" and isinstance(payload, dict):
        _latest_robot_state = dict(payload)
    if _ws_loop is not None and _connected_clients:
        msg = json.dumps({"type": event_type, "payload": payload, "ts": time.time()})
        asyncio.run_coroutine_threadsafe(_send_to_all_clients(msg), _ws_loop)

def start_servers(force_stop_event=None, remote_mode_queue=None, start_exercise_event=None, pause_request_event=None, resume_request_event=None, host="0.0.0.0", http_port=8081, ws_port=8001):
    """
    Start the local MJPEG HTTP server and WS remote control server in background threads.
    :param force_stop_event: threading.Event to toggle on force stop.
    :param remote_mode_queue: queue.Queue to receive switch mode commands.
    :param start_exercise_event: threading.Event to trigger start exercise.
    :param pause_request_event: threading.Event to trigger pause.
    :param resume_request_event: threading.Event to trigger resume.
    :param host: host IP address to bind to (defaults to all interfaces: 0.0.0.0).
    :param http_port: Port number for the HTTP MJPEG stream (default 8081).
    :param ws_port: Port number for the WebSocket server (default 8001).
    """
    global _force_stop_event, _remote_mode_queue, _start_exercise_event, _pause_request_event, _resume_request_event
    _force_stop_event = force_stop_event
    _remote_mode_queue = remote_mode_queue
    _start_exercise_event = start_exercise_event
    _pause_request_event = pause_request_event
    _resume_request_event = resume_request_event

    # Fallback to importing from session_manager if events were not explicitly passed
    if _pause_request_event is None or _resume_request_event is None:
        try:
            import session_manager
            if _pause_request_event is None:
                _pause_request_event = getattr(session_manager, 'pause_request', None)
            if _resume_request_event is None:
                _resume_request_event = getattr(session_manager, 'resume_request', None)
        except Exception:
            pass

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
