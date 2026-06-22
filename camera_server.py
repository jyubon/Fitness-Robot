"""
camera_server.py
Lightweight MJPEG camera streamer for the Fitness Robot.

Run this on the same machine as your robot (Raspberry Pi or PC).

Usage:
    python camera_server.py --port 8080 --camera 0

Then in your robot code (v.1.1.py or person_raspi.py), report the URL:

    from robot_link import post_state

    post_state(
        mode="BICEP_CURLS",
        camera_url="http://192.168.1.50:8080/stream"   # ← use your robot's IP
    )

The dashboard will automatically display the live feed in the "Robot Camera" card.

MJPEG is simple, low-latency, and works directly in an <img> tag.
"""

import argparse
import cv2
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class StreamingHandler(BaseHTTPRequestHandler):
    boundary = b'--frame'

    def do_GET(self):
        if self.path == '/stream':
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
                        continue

                    self.wfile.write(b'--frame\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(jpeg))
                    self.end_headers()
                    self.wfile.write(jpeg.tobytes())
                    self.wfile.write(b'\r\n')

                    # ~15-20 fps target
                    time.sleep(0.05)
            except Exception:
                pass
        else:
            self.send_error(404)
            self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class CameraServer:
    _cap = None
    _frame = None
    _lock = threading.Lock()
    _running = False
    _thread = None

    @classmethod
    def start(cls, camera_index=0, width=640, height=480, fps=30):
        if cls._running:
            return

        cls._cap = cv2.VideoCapture(camera_index)
        if not cls._cap.isOpened():
            raise RuntimeError(f"Cannot open camera index {camera_index}")

        cls._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cls._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cls._cap.set(cv2.CAP_PROP_FPS, fps)

        cls._running = True
        cls._thread = threading.Thread(target=cls._capture_loop, daemon=True)
        cls._thread.start()
        print(f"[camera_server] Camera {camera_index} started ({width}x{height} @ {fps}fps)")

    @classmethod
    def _capture_loop(cls):
        while cls._running:
            ret, frame = cls._cap.read()
            if not ret:
                time.sleep(0.1)
                continue
            with cls._lock:
                cls._frame = frame
            time.sleep(0.01)

    @classmethod
    def get_frame(cls):
        with cls._lock:
            return None if cls._frame is None else cls._frame.copy()

    @classmethod
    def stop(cls):
        cls._running = False
        if cls._thread:
            cls._thread.join(timeout=1)
        if cls._cap:
            cls._cap.release()
        print("[camera_server] Camera stopped")


def run_server(host='0.0.0.0', port=8080, camera_index=0):
    CameraServer.start(camera_index=camera_index)

    server = ThreadedHTTPServer((host, port), StreamingHandler)
    print(f"[camera_server] MJPEG stream available at: http://{host}:{port}/stream")
    print(f"[camera_server] Use your machine's IP in the URL (e.g. http://192.168.1.50:{port}/stream)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        CameraServer.stop()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fitness Robot Camera Streamer (MJPEG)")
    parser.add_argument("--port", type=int, default=8080, help="Port to serve the stream on")
    parser.add_argument("--camera", type=int, default=0, help="Camera index (usually 0)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (0.0.0.0 = all interfaces)")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, camera_index=args.camera)