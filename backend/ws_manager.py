"""
WebSocket connection manager for the Fitness Robot Dashboard.

Handles two channels per user:
- telemetry: Pi publishes live data (reps, form, robot state, etc.); browsers subscribe
- control: bidirectional for force_stop / switch_mode (UI <-> Pi)

All data is real — no dummy messages are generated here.
"""

from typing import Dict, Set
from fastapi import WebSocket
import asyncio
import json


class ConnectionManager:
    """
    Per-user connection manager.
    Keeps sets of active WebSocket connections for telemetry and control.
    """

    def __init__(self):
        # user_id (str) -> set of WebSocket connections
        self.telemetry_connections: Dict[str, Set[WebSocket]] = {}
        self.control_connections: Dict[str, Set[WebSocket]] = {}

        # Simple lock to protect concurrent modifications
        self._lock = asyncio.Lock()

    # --- Telemetry channel (Pi -> everyone) ---

    async def connect_telemetry(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            if user_id not in self.telemetry_connections:
                self.telemetry_connections[user_id] = set()
            self.telemetry_connections[user_id].add(websocket)
        print(f"[WS] Telemetry connected: user={user_id} (total={len(self.telemetry_connections[user_id])})")

    def disconnect_telemetry(self, user_id: str, websocket: WebSocket):
        if user_id in self.telemetry_connections:
            self.telemetry_connections[user_id].discard(websocket)
            if not self.telemetry_connections[user_id]:
                del self.telemetry_connections[user_id]
        print(f"[WS] Telemetry disconnected: user={user_id}")

    async def broadcast_to_user(self, user_id: str, message: dict):
        """
        Send a telemetry message to all browsers (and any other listeners)
        connected to this user's telemetry channel.
        The Pi also receives its own echoes if it stays connected, which is harmless.
        """
        connections = self.telemetry_connections.get(user_id, set())
        if not connections:
            return

        payload = json.dumps(message)
        dead = []
        for ws in list(connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    connections.discard(ws)
                if not connections and user_id in self.telemetry_connections:
                    del self.telemetry_connections[user_id]

    # --- Control channel (UI <-> Pi) ---

    async def connect_control(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            if user_id not in self.control_connections:
                self.control_connections[user_id] = set()
            self.control_connections[user_id].add(websocket)
        print(f"[WS] Control connected: user={user_id} (total={len(self.control_connections[user_id])})")

    def disconnect_control(self, user_id: str, websocket: WebSocket):
        if user_id in self.control_connections:
            self.control_connections[user_id].discard(websocket)
            if not self.control_connections[user_id]:
                del self.control_connections[user_id]
        print(f"[WS] Control disconnected: user={user_id}")

    async def broadcast_control_to_user(self, user_id: str, message: dict):
        """
        Send a control command (or raw message) to everyone on the control channel
        for this user. Typically: UI sends force_stop/switch_mode -> Pi receives it.
        """
        connections = self.control_connections.get(user_id, set())
        if not connections:
            return

        payload = json.dumps(message)
        dead = []
        for ws in list(connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    connections.discard(ws)
                if not connections and user_id in self.control_connections:
                    del self.control_connections[user_id]


# Singleton instance used by main.py and future routers
ws_manager = ConnectionManager()
