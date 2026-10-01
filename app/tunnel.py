import json
import subprocess
import threading
import time
import urllib.request

from app.config import CLOUDFLARED_METRICS_TIMEOUT_S, TUNNEL_START_TIMEOUT_S

METRICS = "127.0.0.1:2000"
VIEWER_ENTRANCE = "http://nginx:82"


class CloudflareTunnel:
    """A Cloudflare quick tunnel to the viewer entrance: cloudflared runs as a child process of the admin."""

    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def open(self):
        """Starts cloudflared if needed and waits until it is up; False if it is not up in time."""
        with self.lock:
            if not self.is_running():
                self.process = subprocess.Popen(
                    ["cloudflared", "tunnel", "--no-autoupdate", "--metrics", METRICS, "--url", VIEWER_ENTRANCE]
                )
            deadline = time.monotonic() + TUNNEL_START_TIMEOUT_S
            while time.monotonic() < deadline:
                if self.is_ready():
                    return True
                time.sleep(1)
            self.stop()
            return False

    def close(self):
        with self.lock:
            self.stop()

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            self.process.wait()
            self.process = None

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def is_ready(self):
        try:
            connected = read_metrics("/ready")["readyConnections"] > 0
        except Exception:  # metrics not up yet; /ready is 503 until the first connection
            return False
        return connected and self.url() is not None

    def url(self):
        if not self.is_running():
            return None
        try:
            hostname = read_metrics("/quicktunnel").get("hostname")
        except Exception:  # not up yet
            return None
        if not hostname:
            return None
        return f"https://{hostname}"


def read_metrics(path):
    with urllib.request.urlopen(f"http://{METRICS}{path}", timeout=CLOUDFLARED_METRICS_TIMEOUT_S) as response:
        return json.load(response)
