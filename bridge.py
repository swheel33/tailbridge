#!/usr/bin/env python3

import argparse
import hmac
import json
import mimetypes
import os
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DEFAULT_PORT = 45871
MAX_ITEM_BYTES = 100 * 1024 * 1024
CLAIM_TTL_SECONDS = 5 * 60
QR_VERSION = 10
INSTALL_URL = "https://www.icloud.com/shortcuts/9fcd515c2a454cd9a18c70cea4898f8d"
IMAGE_TYPES = (
    "image/png",
    "image/jpeg",
    "image/heic",
    "image/heif",
    "image/webp",
    "image/gif",
    "image/tiff",
    "image/bmp",
)
TEXT_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING")


class Bridge:
    def __init__(self, state_dir: Path, port: int):
        self.state_dir = state_dir
        self.state_file = state_dir / "state.json"
        self.port = port
        self.lock = threading.Lock()
        self.clipboard_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stopped = False
        self.claims = {}
        self.server = None
        self.server_thread = None
        self.tailscale_ip = ""
        self.base_url = ""
        self.state = self._load_state()
        self.secret = self.state["secret"]
        self.inbox_dir = state_dir / "inbox"

    def _load_state(self):
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            secret = state.get("secret") if isinstance(state, dict) else None
            if isinstance(secret, str) and len(secret) >= 32:
                return {"secret": secret, "configured": state.get("configured") is True}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        state = {"secret": secrets.token_urlsafe(32), "configured": False}
        self._save_state(state)
        return state

    def _save_state(self, state=None):
        if state is None:
            state = self.state
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(dir=self.state_dir)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                descriptor = -1
                json.dump(state, destination, separators=(",", ":"))
            temporary.replace(self.state_file)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    def emit(self, message):
        with self.output_lock:
            print(json.dumps(message, separators=(",", ":")), flush=True)

    def discover_tailscale_ip(self):
        if not shutil.which("tailscale"):
            raise RuntimeError("Tailscale is not installed")
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        address = result.stdout.strip()
        if result.returncode != 0 or not address.startswith("100."):
            raise RuntimeError((result.stderr or "Tailscale is not connected").strip())
        self.tailscale_ip = address
        self.base_url = f"http://{address}:{self.port}"

    def start_http(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Tailbridge"
            sys_version = ""

            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                bridge.handle_get(self)

            def do_POST(self):
                bridge.handle_post(self)

        try:
            self.server = ThreadingHTTPServer((self.tailscale_ip, self.port), Handler)
        except OSError as error:
            raise RuntimeError(f"Could not listen on {self.tailscale_ip}:{self.port}: {error.strerror}")
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def _clean_expired(self):
        now = time.monotonic()
        self.claims = {key: value for key, value in self.claims.items() if value[1] > now}

    def _shortcut_url(self, shortcut_input):
        encoded = urllib.parse.quote(shortcut_input, safe="")
        return f"shortcuts://run-shortcut?name=Tailbridge&input=text&text={encoded}"

    def _clipboard_types(self):
        if not shutil.which("wl-paste"):
            raise RuntimeError("wl-paste is not installed")
        result = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "Could not inspect the clipboard").strip())
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _read_clipboard(self, media_type):
        result = subprocess.run(
            ["wl-paste", "--type", media_type, "--no-newline"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or "Could not read the clipboard")
        if not result.stdout:
            raise RuntimeError("The clipboard is empty")
        if len(result.stdout) > MAX_ITEM_BYTES:
            raise RuntimeError("Clipboard content is larger than 100 MiB")
        return result.stdout

    def _local_clipboard_file(self, types):
        uri_type = next((item for item in ("text/uri-list", "x-special/gnome-copied-files") if item in types), None)
        if uri_type is None:
            return None
        payload = self._read_clipboard(uri_type).decode("utf-8", "replace")
        lines = [line.strip() for line in payload.splitlines() if line.strip() and not line.startswith("#")]
        if uri_type == "x-special/gnome-copied-files" and lines and lines[0] in ("copy", "cut"):
            lines = lines[1:]
        local_paths = []
        for line in lines:
            parsed = urllib.parse.urlsplit(line)
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                continue
            local_paths.append(Path(urllib.parse.unquote(parsed.path)))
        if not local_paths:
            return None
        if len(local_paths) != 1:
            raise RuntimeError("Copy one file at a time")
        path = local_paths[0]
        if path.is_dir():
            raise RuntimeError("Folders are not supported")
        if not path.is_file():
            raise RuntimeError("The copied file is no longer available")
        if path.stat().st_size > MAX_ITEM_BYTES:
            raise RuntimeError("Clipboard content is larger than 100 MiB")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return {"kind": "file", "type": media_type, "name": path.name, "path": path}

    def current_clipboard_item(self):
        types = self._clipboard_types()
        if not types:
            raise RuntimeError("The clipboard is empty")

        file_item = self._local_clipboard_file(types)
        if file_item is not None:
            return file_item

        media_type = next((item for item in IMAGE_TYPES if item in types), None)
        if media_type is not None:
            extension = mimetypes.guess_extension(media_type) or ".img"
            if extension == ".jpe":
                extension = ".jpg"
            return {
                "kind": "image",
                "type": media_type,
                "name": f"Tailbridge{extension}",
                "data": self._read_clipboard(media_type),
            }

        media_type = next((item for item in TEXT_TYPES if item in types), None)
        if media_type is not None:
            data = self._read_clipboard(media_type)
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RuntimeError("The clipboard does not contain UTF-8 text") from error
            return {"kind": "text", "type": "text/plain; charset=utf-8", "name": "", "data": data}

        raise RuntimeError("This clipboard format is not supported")

    def _qr_matrix(self, value):
        if not shutil.which("qrencode"):
            raise RuntimeError("qrencode is not installed")
        result = subprocess.run(
            [
                "qrencode",
                "--type", "ASCII",
                "--margin", "4",
                "--symversion", str(QR_VERSION),
                "--strict-version",
                "--output", "-",
            ],
            input=value,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "Could not generate QR code").strip())
        rows = [
            "".join("1" if "#" in line[index:index + 2] else "0" for index in range(0, len(line), 2))
            for line in result.stdout.splitlines()
        ]
        if not rows or any(len(row) != len(rows) for row in rows):
            raise RuntimeError("qrencode returned an invalid matrix")
        return rows

    def create_setup(self):
        inbox_url = f"{self.base_url}/v1/inbox/{self.secret}"
        return self._qr_matrix(self._shortcut_url(f"tailbridge-setup:{inbox_url}"))

    def create_install(self):
        return self._qr_matrix(INSTALL_URL)

    def create_claim(self):
        item = self.current_clipboard_item()
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.claims.clear()
            self.claims[token] = (item, time.monotonic() + CLAIM_TTL_SECONDS)
        claim_url = f"{self.base_url}/v1/claims/{token}"
        return self._qr_matrix(self._shortcut_url(claim_url)), item

    def handle_command(self, command):
        request_id = command.get("id")
        action = command.get("action")
        try:
            if action == "install":
                rows = self.create_install()
                result = {"id": request_id, "ok": True, "kind": "install", "rows": rows}
            elif action == "setup":
                rows = self.create_setup()
                result = {"id": request_id, "ok": True, "kind": "setup", "rows": rows}
            elif action == "claim":
                rows, item = self.create_claim()
                result = {
                    "id": request_id,
                    "ok": True,
                    "kind": "claim",
                    "itemKind": item["kind"],
                    "itemName": item["name"],
                    "rows": rows,
                }
            elif action == "configured":
                self.state["configured"] = True
                self._save_state()
                result = {"id": request_id, "ok": True, "configured": True}
            elif action == "clear":
                with self.lock:
                    self.claims.clear()
                result = {"id": request_id, "ok": True}
            else:
                raise RuntimeError("Unknown bridge action")
        except Exception as error:
            result = {"id": request_id, "ok": False, "error": str(error)}
        self.emit(result)

    def _authorized(self, request):
        expected = f"/v1/inbox/{self.secret}"
        supplied = urllib.parse.urlsplit(request.path).path
        return hmac.compare_digest(expected, supplied)

    def _setup_authorized(self, path):
        return hmac.compare_digest(f"/v1/setup/{self.secret}", path)

    def _respond(self, request, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "application/json; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.end_headers()
        request.wfile.write(body)

    def _respond_text(self, request, status, text):
        body = text.encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "text/plain; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.end_headers()
        request.wfile.write(body)

    def _respond_item(self, request, item):
        path = item.get("path")
        if path is not None:
            try:
                size = path.stat().st_size
            except OSError:
                self._respond(request, 404, {"error": "Clipboard file is no longer available"})
                return
            if size > MAX_ITEM_BYTES:
                self._respond(request, 413, {"error": "Clipboard content is larger than 100 MiB"})
                return
            body = None
        else:
            body = item["data"]
            size = len(body)

        request.send_response(200)
        request.send_header("Content-Type", item["type"])
        request.send_header("Content-Length", str(size))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        if item["name"]:
            disposition = "attachment" if item["kind"] == "file" else "inline"
            safe_name = "".join(
                character if character.isascii() and character.isprintable() and character != '"' else "_"
                for character in item["name"]
            )
            encoded_name = urllib.parse.quote(item["name"], safe="")
            request.send_header(
                "Content-Disposition",
                f"{disposition}; filename=\"{safe_name}\"; filename*=UTF-8''{encoded_name}",
            )
        request.end_headers()
        if path is None:
            request.wfile.write(body)
            return
        with path.open("rb") as source:
            shutil.copyfileobj(source, request.wfile, length=1024 * 1024)

    def _safe_filename(self, supplied, media_type):
        decoded = urllib.parse.unquote(str(supplied or ""))
        name = Path(decoded).name
        name = "".join(character if character.isprintable() and character not in "/\\" else "_" for character in name)
        if not name:
            extension = mimetypes.guess_extension(media_type) or ".bin"
            name = f"Tailbridge{extension}"
        return name[:255]

    def _clear_inbox(self):
        if self.inbox_dir.exists():
            shutil.rmtree(self.inbox_dir)

    def _copy_bytes(self, data, media_type):
        if not shutil.which("wl-copy"):
            raise RuntimeError("wl-copy is not installed")
        result = subprocess.run(
            ["wl-copy", "--type", media_type],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Could not update the clipboard")

    def _receive_file(self, request, length, media_type, supplied_name):
        name = self._safe_filename(supplied_name, media_type)
        self.inbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        transfer_dir = self.inbox_dir / secrets.token_urlsafe(12)
        transfer_dir.mkdir(mode=0o700)
        temporary = transfer_dir / ".upload"
        try:
            remaining = length
            with temporary.open("wb") as destination:
                while remaining:
                    chunk = request.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("Upload ended before all content was received")
                    destination.write(chunk)
                    remaining -= len(chunk)
            os.chmod(temporary, 0o600)
            destination = transfer_dir / name
            temporary.replace(destination)
            self._copy_bytes((destination.resolve().as_uri() + "\r\n").encode("utf-8"), "text/uri-list")
        except Exception:
            shutil.rmtree(transfer_dir, ignore_errors=True)
            raise

        for previous in self.inbox_dir.iterdir():
            if previous == transfer_dir:
                continue
            try:
                if previous.is_dir() and not previous.is_symlink():
                    shutil.rmtree(previous)
                else:
                    previous.unlink()
            except OSError:
                pass
        return name

    def handle_get(self, request):
        path = urllib.parse.urlsplit(request.path).path
        if path == "/v1/health":
            self._respond(request, 200, {"ok": True})
            return
        if self._setup_authorized(path):
            self._respond_text(request, 200, "tailbridge-ready")
            return
        if path.startswith("/v1/claims/"):
            token = path.removeprefix("/v1/claims/")
            with self.lock:
                self._clean_expired()
                claim = self.claims.pop(token, None)
            if claim is None:
                self._respond(request, 404, {"error": "Clipboard code is invalid or expired"})
                return
            self._respond_item(request, claim[0])
            return
        self._respond(request, 404, {"error": "Not found"})

    def handle_post(self, request):
        if not self._authorized(request):
            self._respond(request, 404, {"error": "Not found"})
            return
        try:
            length = int(request.headers.get("Content-Length", ""))
        except ValueError:
            self._respond(request, 411, {"error": "Content-Length is required"})
            return
        if length <= 0:
            self._respond(request, 400, {"error": "The clipboard is empty"})
            return
        if length > MAX_ITEM_BYTES:
            self._respond(request, 413, {"error": "Clipboard content is larger than 100 MiB"})
            return

        kind = str(request.headers.get("X-Tailbridge-Kind", "")).lower()
        media_type = str(request.headers.get("Content-Type", "application/octet-stream")).split(";", 1)[0].lower()
        if kind not in ("text", "image", "file"):
            self._respond(request, 400, {"error": "Install the current Tailbridge Shortcut"})
            return
        if kind == "image" and not media_type.startswith("image/"):
            self._respond(request, 400, {"error": "Expected image clipboard content"})
            return

        try:
            with self.clipboard_lock:
                if kind == "file":
                    name = self._receive_file(request, length, media_type, request.headers.get("X-Tailbridge-Name"))
                else:
                    data = request.rfile.read(length)
                    if len(data) != length:
                        raise RuntimeError("Upload ended before all content was received")
                    if kind == "text":
                        try:
                            data.decode("utf-8")
                        except UnicodeDecodeError as error:
                            raise RuntimeError("Clipboard text is not UTF-8") from error
                        media_type = "text/plain;charset=utf-8"
                    self._copy_bytes(data, media_type)
                    self._clear_inbox()
                    name = ""
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self._respond(request, 503, {"error": str(error) or "Could not update the clipboard"})
            return
        self._respond(request, 200, {"ok": True, "kind": kind, "name": name})

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.stop_event.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def run(self):
        self.discover_tailscale_ip()
        self.start_http()
        self.emit({
            "event": "ready",
            "baseUrl": self.base_url,
            "status": "ready",
            "detail": f"Listening on {self.tailscale_ip}:{self.port}",
            "configured": self.state["configured"],
        })

        while not self.stop_event.is_set():
            readable, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not readable:
                continue
            line = sys.stdin.readline()
            if line == "":
                break
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                self.emit({"ok": False, "error": "Malformed bridge command"})
                continue
            self.handle_command(command)
        self.stop()


def main():
    parser = argparse.ArgumentParser(description="Tailbridge clipboard service")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy" / "tailbridge",
    )
    args = parser.parse_args()
    bridge = Bridge(args.state_dir, args.port)

    def stop(_signum, _frame):
        bridge.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        bridge.run()
    except Exception as error:
        bridge.emit({"event": "fatal", "error": str(error)})
        raise SystemExit(1)
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
