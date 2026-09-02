#!/usr/bin/env python3

import argparse
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import select
import selectors
import shutil
import signal
import socket
import stat
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
QR_MATRIX_SIZE = 17 + 4 * QR_VERSION + 8
MAX_HTTP_HANDLERS = 4
HEADER_TIMEOUT_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 120
MAX_STATE_BYTES = 4096
MAX_DIAGNOSTIC_BYTES = 8192
MAX_TYPES_BYTES = 64 * 1024
MAX_QR_BYTES = 64 * 1024
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


class ProcessOutputLimit(RuntimeError):
    pass


def _stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _run_process(args, *, input_data=None, stdout_limit=None, stderr_limit=MAX_DIAGNOSTIC_BYTES, timeout):
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if stdout_limit is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE if stderr_limit is not None else subprocess.DEVNULL,
        start_new_session=True,
    )
    selector = None
    try:
        selector = selectors.DefaultSelector()
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        limits = {"stdout": stdout_limit, "stderr": stderr_limit}
        input_view = memoryview(input_data) if input_data is not None else None
        input_offset = 0
        deadline = time.monotonic() + timeout

        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(args, timeout)
            for key, _events in events:
                stream = key.fileobj
                if key.data == "stdin":
                    if input_offset >= len(input_view):
                        selector.unregister(stream)
                        stream.close()
                        continue
                    try:
                        written = os.write(stream.fileno(), input_view[input_offset:input_offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    input_offset += written
                    continue

                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                output = outputs[key.data]
                if len(output) + len(chunk) > limits[key.data]:
                    raise ProcessOutputLimit(f"{key.data} exceeded its limit")
                output.extend(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(args, returncode, bytes(outputs["stdout"]), bytes(outputs["stderr"]))
    except BaseException:
        _stop_process_group(process)
        raise
    finally:
        if selector is not None:
            selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class):
        self._handler_slots = threading.BoundedSemaphore(MAX_HTTP_HANDLERS)
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address):
        if not self._handler_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()

    def handle_error(self, _request, _client_address):
        return


class Bridge:
    def __init__(self, state_dir: Path, port: int):
        self.state_dir = Path(os.path.abspath(os.path.expanduser(state_dir)))
        self.state_file = self.state_dir / "state.json"
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
        self._ensure_private_directory(self.state_dir)
        self.state = self._load_state()
        self.secret = self.state["secret"]
        self.inbox_dir = self.state_dir / "inbox"

    def _ensure_private_directory(self, path):
        path = Path(path)
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                details = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                details = current.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise RuntimeError(f"Unsafe directory path: {current}")
            if details.st_uid not in (0, os.geteuid()):
                raise RuntimeError(f"Directory is owned by another user: {current}")
            if current != path and details.st_mode & 0o022:
                raise RuntimeError(f"Directory is writable by other users: {current}")
        details = path.lstat()
        if details.st_uid != os.geteuid():
            raise RuntimeError(f"Directory is not owned by the current user: {path}")
        if stat.S_IMODE(details.st_mode) != 0o700:
            path.chmod(0o700)

    def _load_state(self):
        try:
            descriptor = os.open(
                self.state_file,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
                    raise RuntimeError("Tailbridge state file is not a private regular file")
                if stat.S_IMODE(details.st_mode) != 0o600:
                    os.fchmod(descriptor, 0o600)
                chunks = []
                remaining = MAX_STATE_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
            finally:
                os.close(descriptor)
            if len(raw) > MAX_STATE_BYTES:
                raise RuntimeError("Tailbridge state file is too large")
            state = json.loads(raw.decode("utf-8"))
            secret = state.get("secret") if isinstance(state, dict) else None
            if (
                isinstance(secret, str)
                and len(secret) == 43
                and all(character.isascii() and (character.isalnum() or character in "_-") for character in secret)
                and isinstance(state.get("configured"), bool)
            ):
                return {"secret": secret, "configured": state.get("configured") is True}
            raise RuntimeError("Tailbridge state file has invalid contents")
        except FileNotFoundError:
            pass
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
            raise RuntimeError(f"Could not safely read Tailbridge state: {error}") from error

        state = {"secret": secrets.token_urlsafe(32), "configured": False}
        self._save_state(state)
        return state

    def _save_state(self, state=None):
        if state is None:
            state = self.state
        self._ensure_private_directory(self.state_dir)
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
        result = _run_process(["tailscale", "ip", "-4"], stdout_limit=4096, timeout=10)
        address = result.stdout.decode("utf-8", "replace").strip()
        diagnostic = result.stderr.decode("utf-8", "replace").strip()
        try:
            parsed_address = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            parsed_address = None
        if (
            result.returncode != 0
            or parsed_address is None
            or parsed_address not in ipaddress.IPv4Network("100.64.0.0/10")
        ):
            raise RuntimeError(diagnostic or "Tailscale is not connected")
        self.tailscale_ip = address
        self.base_url = f"http://{address}:{self.port}"

    def start_http(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Tailbridge"
            sys_version = ""

            def setup(self):
                super().setup()
                self._deadline_lock = threading.Lock()
                self._headers_complete = False
                self._request_timer = None
                self.connection.settimeout(HEADER_TIMEOUT_SECONDS)
                self._header_timer = threading.Timer(HEADER_TIMEOUT_SECONDS, self._expire_headers)
                self._header_timer.daemon = True
                self._header_timer.start()

            def _expire_headers(self):
                with self._deadline_lock:
                    if self._headers_complete:
                        return
                    self._shutdown_connection()

            def _expire_request(self):
                self._shutdown_connection()

            def _shutdown_connection(self):
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def parse_request(self):
                try:
                    parsed = super().parse_request()
                finally:
                    with self._deadline_lock:
                        self._headers_complete = True
                        self._header_timer.cancel()
                if parsed:
                    self.close_connection = True
                    self.request_deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
                    self._request_timer = threading.Timer(REQUEST_TIMEOUT_SECONDS, self._expire_request)
                    self._request_timer.daemon = True
                    self._request_timer.start()
                return parsed

            def finish(self):
                self._header_timer.cancel()
                if self._request_timer is not None:
                    self._request_timer.cancel()
                super().finish()

            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                bridge.handle_get(self)

            def do_POST(self):
                bridge.handle_post(self)

        try:
            self.server = BoundedThreadingHTTPServer((self.tailscale_ip, self.port), Handler)
        except OSError as error:
            raise RuntimeError(f"Could not listen on {self.tailscale_ip}:{self.port}: {error.strerror}")
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def _clean_expired(self):
        now = time.monotonic()
        expired = [key for key, value in self.claims.items() if value[1] <= now]
        for key in expired:
            self._close_item(self.claims.pop(key)[0])

    def _clear_claims(self):
        for item, _expires in self.claims.values():
            self._close_item(item)
        self.claims.clear()

    @staticmethod
    def _close_item(item):
        stream = item.get("stream")
        if stream is not None:
            stream.close()

    def _shortcut_url(self, shortcut_input):
        encoded = urllib.parse.quote(shortcut_input, safe="")
        return f"shortcuts://run-shortcut?name=Tailbridge&input=text&text={encoded}"

    def _clipboard_types(self):
        if not shutil.which("wl-paste"):
            raise RuntimeError("wl-paste is not installed")
        result = _run_process(["wl-paste", "--list-types"], stdout_limit=MAX_TYPES_BYTES, timeout=5)
        output = result.stdout.decode("utf-8", "replace")
        if result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(diagnostic or "Could not inspect the clipboard")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _read_clipboard(self, media_type):
        try:
            result = _run_process(
                ["wl-paste", "--type", media_type, "--no-newline"],
                stdout_limit=MAX_ITEM_BYTES,
                timeout=15,
            )
        except ProcessOutputLimit as error:
            raise RuntimeError("Clipboard content is larger than 100 MiB") from error
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or "Could not read the clipboard")
        if not result.stdout:
            raise RuntimeError("The clipboard is empty")
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
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise RuntimeError("The copied file is no longer available") from error
        snapshot = None
        try:
            before = os.fstat(descriptor)
            if stat.S_ISDIR(before.st_mode):
                raise RuntimeError("Folders are not supported")
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError("The copied item is not a regular file")
            if before.st_size > MAX_ITEM_BYTES:
                raise RuntimeError("Clipboard content is larger than 100 MiB")
            snapshot = tempfile.TemporaryFile()
            size = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_ITEM_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ITEM_BYTES:
                    raise RuntimeError("Clipboard content is larger than 100 MiB")
                snapshot.write(chunk)
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("The copied file changed while it was being read")
            snapshot.seek(0)
        except Exception:
            if snapshot is not None:
                snapshot.close()
            raise
        finally:
            os.close(descriptor)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return {"kind": "file", "type": media_type, "name": path.name, "stream": snapshot, "size": size}

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
        result = _run_process(
            [
                "qrencode",
                "--type", "ASCII",
                "--margin", "4",
                "--symversion", str(QR_VERSION),
                "--strict-version",
                "--output", "-",
            ],
            input_data=value.encode("utf-8"),
            stdout_limit=MAX_QR_BYTES,
            timeout=5,
        )
        if result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(diagnostic or "Could not generate QR code")
        output = result.stdout.decode("ascii", "replace")
        rows = [
            "".join("1" if "#" in line[index:index + 2] else "0" for index in range(0, len(line), 2))
            for line in output.splitlines()
        ]
        if len(rows) != QR_MATRIX_SIZE or any(len(row) != QR_MATRIX_SIZE for row in rows):
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
        try:
            claim_url = f"{self.base_url}/v1/claims/{token}"
            rows = self._qr_matrix(self._shortcut_url(claim_url))
            with self.lock:
                self._clear_claims()
                self.claims[token] = (item, time.monotonic() + CLAIM_TTL_SECONDS)
            return rows, item
        except Exception:
            self._close_item(item)
            raise

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
                    self._clear_claims()
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
        request.send_header("Connection", "close")
        request.end_headers()
        self._write_body(request, body)

    def _respond_text(self, request, status, text):
        body = text.encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "text/plain; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header("Connection", "close")
        request.end_headers()
        self._write_body(request, body)

    @staticmethod
    def _remaining_request_time(request, maximum=None):
        remaining = request.request_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request timed out")
        return min(remaining, maximum) if maximum is not None else remaining

    def _write_body(self, request, body):
        view = memoryview(body)
        for offset in range(0, len(view), 1024 * 1024):
            request.connection.settimeout(self._remaining_request_time(request))
            request.wfile.write(view[offset:offset + 1024 * 1024])

    def _respond_item(self, request, item):
        stream = item.get("stream")
        body = None if stream is not None else item["data"]
        size = item["size"] if stream is not None else len(body)

        request.send_response(200)
        request.send_header("Content-Type", item["type"])
        request.send_header("Content-Length", str(size))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header("Connection", "close")
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
        if stream is None:
            self._write_body(request, body)
            return
        stream.seek(0)
        remaining_bytes = size
        while remaining_bytes:
            request.connection.settimeout(self._remaining_request_time(request))
            chunk = stream.read(min(1024 * 1024, remaining_bytes))
            if not chunk:
                raise OSError("Clipboard snapshot ended unexpectedly")
            request.wfile.write(chunk)
            remaining_bytes -= len(chunk)

    def _safe_filename(self, supplied, media_type):
        decoded = urllib.parse.unquote(str(supplied or ""))
        name = Path(decoded).name
        name = "".join(character if character.isprintable() and character not in "/\\" else "_" for character in name)
        if name in ("", ".", ".."):
            extension = mimetypes.guess_extension(media_type) or ".bin"
            name = f"Tailbridge{extension}"
        while len(name.encode("utf-8")) > 240:
            name = name[:-1]
        return name

    def _clear_inbox(self):
        self._ensure_private_directory(self.inbox_dir)
        shutil.rmtree(self.inbox_dir)

    def _copy_bytes(self, data, media_type, timeout=15):
        if not shutil.which("wl-copy"):
            raise RuntimeError("wl-copy is not installed")
        result = _run_process(
            ["wl-copy", "--type", media_type],
            input_data=data,
            stdout_limit=None,
            stderr_limit=None,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError("Could not update the clipboard")

    def _receive_file(self, request, length, media_type, supplied_name):
        name = self._safe_filename(supplied_name, media_type)
        self._ensure_private_directory(self.inbox_dir)
        transfer_dir = self.inbox_dir / secrets.token_urlsafe(12)
        transfer_dir.mkdir(mode=0o700)
        temporary = transfer_dir / ".upload"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                self._read_request_body(request, length, destination)
            destination = transfer_dir / name
            temporary.replace(destination)
            self._copy_bytes(
                (destination.resolve().as_uri() + "\r\n").encode("utf-8"),
                "text/uri-list",
                self._remaining_request_time(request, 15),
            )
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

    def _read_request_body(self, request, length, destination=None):
        remaining = length
        body = bytearray() if destination is None else None
        while remaining:
            request.connection.settimeout(self._remaining_request_time(request))
            chunk = request.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError("Upload ended before all content was received")
            if destination is None:
                body.extend(chunk)
            else:
                destination.write(chunk)
            remaining -= len(chunk)
        return bytes(body) if body is not None else None

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
            try:
                self._respond_item(request, claim[0])
            finally:
                self._close_item(claim[0])
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
            if not self.clipboard_lock.acquire(timeout=self._remaining_request_time(request)):
                raise TimeoutError("Request timed out waiting for the clipboard")
            try:
                if kind == "file":
                    name = self._receive_file(request, length, media_type, request.headers.get("X-Tailbridge-Name"))
                else:
                    data = self._read_request_body(request, length)
                    if kind == "text":
                        try:
                            data.decode("utf-8")
                        except UnicodeDecodeError as error:
                            raise RuntimeError("Clipboard text is not UTF-8") from error
                        media_type = "text/plain;charset=utf-8"
                    self._copy_bytes(data, media_type, self._remaining_request_time(request, 15))
                    self._clear_inbox()
                    name = ""
            finally:
                self.clipboard_lock.release()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self._respond(request, 503, {"error": str(error) or "Could not update the clipboard"})
            return
        self._respond(request, 200, {"ok": True, "kind": kind, "name": name})

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.stop_event.set()
        with self.lock:
            self._clear_claims()
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
            with self.lock:
                self._clean_expired()
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
    try:
        bridge = Bridge(args.state_dir, args.port)
    except Exception as error:
        print(json.dumps({"event": "fatal", "error": str(error)}, separators=(",", ":")), flush=True)
        raise SystemExit(1)

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
