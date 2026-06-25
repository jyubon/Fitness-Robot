"""Simple MJPEG streamer for exposing a laptop webcam to the browser.

Run this separately while testing on your laptop:

    python mjpeg_streamer.py

Then the dashboard can load http://<your-host>:8080/stream

This is intentionally minimal and uses Flask + OpenCV.
"""
from threading import Thread
from flask import Flask, Response
import cv2

CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
JPEG_QUALITY = 80
PORT = 8080

app = Flask(__name__)

def gen_frames(index=CAMERA_INDEX):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera index {index}")
    
    # Set camera resolution
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 15)  # Lower FPS for better performance over network
    
    try:
        while True:
            success, frame = cap.read()
            if not success:
                break
            # Encode with quality setting to reduce bandwidth
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    finally:
        cap.release()


@app.route('/stream')
def stream():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


def run():
    app.run(host='0.0.0.0', port=PORT, threaded=True)


if __name__ == '__main__':
    print(f"Starting MJPEG streamer on port {PORT} (camera index {CAMERA_INDEX})")
    print(f"  Frame size: {CAMERA_WIDTH}x{CAMERA_HEIGHT} @ 15 FPS")
    print(f"  JPEG quality: {JPEG_QUALITY}")
    run()
