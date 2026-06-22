#!/usr/bin/env python3
"""
camera_server_pi.py
MJPEG camera streamer optimized for Raspberry Pi (using picamera2).

This is the recommended way on Raspberry Pi 4 / 5 with modern Pi OS (Bullseye/Bookworm).

Install dependencies (one time):
    sudo apt update
    sudo apt install -y python3-picamera2 python3-opencv

Run:
    python3 camera_server_pi.py --port 8080 --width 640 --height 480 --fps 15

Then report the URL to your dashboard from the robot code:

    from robot_link import post_state

    post_state(
        mode="PERSON_TRACKING",
        camera_url="http://192.168.1.50:8080/stream"   # ← use your Pi's IP
    )

The dashboard will automatically show the live feed.
"""

import argparse
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    from picamera2 import Picamera2
except ImportError:
    print("ERROR: picamera2 is not installed.")
    print("Run: sudo apt install -y python3-picamera2")
    exit(1)

import cv2


class StreamingHandler(BaseHTTPRequestHandler):
    boundary = b'--frame'

    def do_GET(self):
        if self.path in ('/stream', '/'):
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()

            try:
                while True:
                    frame = CameraServer.get_frame()
                    if frame is None:
                        time.sleep(0.05)
                        continue

                    # Encode as JPEG
                    success, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if not success:
                        time.sleep(0.05)
                        continue

                    self.wfile.write(b'--frame\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(jpeg))
                    self.end_headers()
                    self.wfile.write(jpeg.tobytes())
                    self.wfile.write(b'\r\n')

                    time.sleep(1.0 / CameraServer.fps)
            except Exception:
                pass
        else:
            self.send_error(404)
            self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class CameraServer:
    _picam = None
    _frame = None
    _lock = __import__('threading').Lock()
    _running = False
    _thread = None
    fps = 15

    @classmethod
    def start(cls, width=640, height=480, fps=15):
        if cls._running:
            return

        cls.fps = max(5, min(fps, 30))

        cls._picam = Picamera2()
        config = cls._picam.create_preview_configuration(
            main={"size": (width, height), "format": "RGB888"}
        )
        cls._picam.configure(config)
        cls._picam.start()

        cls._running = True

        import threading
        cls._thread = threading.Thread(target=cls._capture_loop, daemon=True)
        cls._thread.start()

        print(f"[camera_server_pi] Started {width}x{height} @ {cls.fps}fps using picamera2")

    @classmethod
    def _capture_loop(cls):
        while cls._running:
            try:
                frame = cls._picam.capture_array()
                with cls._lock:
                    cls._frame = frame
            except Exception:
                pass
            time.sleep(1.0 / cls.fps)

    @classmethod
    def get_frame(cls):
        with cls._lock:
            return None if cls._frame is None else cls._frame.copy()

    @classmethod
    def stop(cls):
        cls._running = False
        if cls._thread:
            cls._thread.join(timeout=1)
        if cls._picam:
            cls._picam.stop()
            cls._picam.close()
        print("[camera_server_pi] Camera stopped")


def run_server(host='0.0.0.0', port=8080, width=640, height=480, fps=15):
    CameraServer.start(width=width, height=height, fps=fps)

    server = ThreadedHTTPServer((host, port), StreamingHandler)
    print(f"[camera_server_pi] MJPEG stream ready at: http://{host}:{port}/stream")
    print(f"[camera_server_pi] From other machines use your Pi IP, e.g. http://192.168.x.x:{port}/stream")
    print("[camera_server_pi] Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        CameraServer.stop()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fitness Robot Camera (Raspberry Pi - picamera2 MJPEG)")
    parser.add_argument("--port", type=int, default=8080, help="Port to serve on (default 8080)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=15)
    args = parser.parse_args()

    run_server(
        host=args.host,
        port=args.port,
        width=args.width,
        height=args.height,
        fps=args.fps
    )
