import http.client
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import bridge


ROOT = Path(__file__).resolve().parents[1]


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.state_dir = Path(self.temporary.name) / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def make_bridge(self):
        return bridge.Bridge(self.state_dir, 0)


class ProcessTests(BridgeTestCase):
    def test_process_output_is_bounded(self):
        result = bridge._run_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abcd')"],
            stdout_limit=4,
            timeout=2,
        )
        self.assertEqual(result.stdout, b"abcd")

        with self.assertRaises(bridge.ProcessOutputLimit):
            bridge._run_process(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abcde')"],
                stdout_limit=4,
                timeout=2,
            )

    def test_timeout_kills_the_process_group(self):
        child_pid_file = Path(self.temporary.name) / "child.pid"
        command = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
            "time.sleep(10)"
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            bridge._run_process(
                [sys.executable, "-c", command, str(child_pid_file)],
                stdout_limit=1,
                timeout=0.5,
            )
        self.assertLess(time.monotonic() - started, 2)
        child_pid = int(child_pid_file.read_text())
        deadline = time.monotonic() + 1
        process_path = Path(f"/proc/{child_pid}")
        while time.monotonic() < deadline:
            if not process_path.exists():
                break
            try:
                if (process_path / "stat").read_text().split()[2] == "Z":
                    break
            except FileNotFoundError:
                break
            time.sleep(0.01)
        else:
            self.fail("subprocess descendant survived process-group cleanup")


class StateTests(BridgeTestCase):
    def test_existing_state_mode_is_repaired_before_use(self):
        instance = self.make_bridge()
        instance.state_file.chmod(0o644)

        reloaded = self.make_bridge()

        self.assertEqual(reloaded.secret, instance.secret)
        self.assertEqual(stat.S_IMODE(reloaded.state_file.stat().st_mode), 0o600)

    def test_symlinked_state_file_is_rejected(self):
        instance = self.make_bridge()
        target = Path(self.temporary.name) / "target.json"
        instance.state_file.replace(target)
        instance.state_file.symlink_to(target)

        with self.assertRaisesRegex(RuntimeError, "safely read Tailbridge state"):
            self.make_bridge()

    def test_fifo_state_file_is_rejected_without_blocking(self):
        instance = self.make_bridge()
        instance.state_file.unlink()
        os.mkfifo(instance.state_file)

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "private regular file"):
            self.make_bridge()
        self.assertLess(time.monotonic() - started, 1)


class ClipboardFileTests(BridgeTestCase):
    def test_fifo_is_rejected_without_blocking(self):
        instance = self.make_bridge()
        fifo = Path(self.temporary.name) / "clipboard"
        os.mkfifo(fifo)

        with mock.patch.object(instance, "_read_clipboard", return_value=fifo.as_uri().encode()):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                instance._local_clipboard_file(["text/uri-list"])
        self.assertLess(time.monotonic() - started, 1)

    def test_claim_uses_a_stable_file_snapshot(self):
        instance = self.make_bridge()
        source = Path(self.temporary.name) / "clipboard.txt"
        source.write_bytes(b"original")

        with mock.patch.object(instance, "_read_clipboard", return_value=source.as_uri().encode()):
            item = instance._local_clipboard_file(["text/uri-list"])
        source.write_bytes(b"replacement")

        try:
            self.assertEqual(item["size"], len(b"original"))
            self.assertEqual(item["stream"].read(), b"original")
        finally:
            instance._close_item(item)

    def test_expired_claim_snapshot_is_closed(self):
        instance = self.make_bridge()
        snapshot = tempfile.TemporaryFile()
        instance.claims["expired"] = ({"stream": snapshot}, time.monotonic() - 1)

        instance._clean_expired()

        self.assertTrue(snapshot.closed)
        self.assertFalse(instance.claims)


class HttpTests(BridgeTestCase):
    def test_slow_headers_release_bounded_handler_slots(self):
        instance = self.make_bridge()
        instance.tailscale_ip = "127.0.0.1"
        clients = []
        with mock.patch.object(bridge, "HEADER_TIMEOUT_SECONDS", 0.5):
            instance.start_http()
            port = instance.server.server_address[1]
            try:
                for _ in range(bridge.MAX_HTTP_HANDLERS):
                    client = socket.create_connection(("127.0.0.1", port), timeout=1)
                    client.sendall(b"GET /v1/health HTTP/1.1\r\nX-Slow: ")
                    clients.append(client)
                deadline = time.monotonic() + 0.25
                while instance.server._handler_slots.acquire(blocking=False):
                    instance.server._handler_slots.release()
                    if time.monotonic() >= deadline:
                        self.fail("slow headers did not occupy all bounded handler slots")
                    time.sleep(0.01)

                rejected = socket.create_connection(("127.0.0.1", port), timeout=1)
                rejected.settimeout(1)
                self.assertEqual(rejected.recv(1), b"")
                rejected.close()

                deadline = time.monotonic() + 2
                while True:
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                        connection.request("GET", "/v1/health")
                        response = connection.getresponse()
                        response.read()
                        connection.close()
                        self.assertEqual(response.status, 200)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("slow header handlers did not release their slots")
            finally:
                for client in clients:
                    client.close()
                instance.stop()

    def test_clipboard_lock_wait_uses_request_deadline(self):
        instance = self.make_bridge()
        instance.tailscale_ip = "127.0.0.1"
        with mock.patch.object(bridge, "REQUEST_TIMEOUT_SECONDS", 0.2):
            instance.start_http()
            port = instance.server.server_address[1]
            instance.clipboard_lock.acquire()
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=2)
                request = (
                    f"POST /v1/inbox/{instance.secret} HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    "Content-Length: 1\r\n"
                    "Content-Type: text/plain\r\n"
                    "X-Tailbridge-Kind: text\r\n\r\n"
                    "x"
                ).encode()
                started = time.monotonic()
                connection.sendall(request)
                while connection.recv(4096):
                    pass
                self.assertLess(time.monotonic() - started, 0.75)
                connection.close()
            finally:
                instance.clipboard_lock.release()
                instance.stop()

    def test_slow_body_cannot_extend_absolute_request_deadline(self):
        instance = self.make_bridge()
        instance.tailscale_ip = "127.0.0.1"
        with mock.patch.object(bridge, "REQUEST_TIMEOUT_SECONDS", 0.25):
            instance.start_http()
            port = instance.server.server_address[1]
            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
            connection.settimeout(1)
            stop_drip = threading.Event()

            def drip_body():
                while not stop_drip.wait(0.05):
                    try:
                        connection.sendall(b"x")
                    except OSError:
                        return

            try:
                headers = (
                    f"POST /v1/inbox/{instance.secret} HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    "Content-Length: 100\r\n"
                    "Content-Type: text/plain\r\n"
                    "X-Tailbridge-Kind: text\r\n\r\n"
                ).encode()
                connection.sendall(headers)
                dripper = threading.Thread(target=drip_body)
                dripper.start()
                started = time.monotonic()
                try:
                    while connection.recv(4096):
                        pass
                except OSError:
                    pass
                self.assertLess(time.monotonic() - started, 1)
                stop_drip.set()
                dripper.join(timeout=1)
                self.assertFalse(dripper.is_alive())

                health = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                health.request("GET", "/v1/health")
                response = health.getresponse()
                response.read()
                health.close()
                self.assertEqual(response.status, 200)
            finally:
                stop_drip.set()
                connection.close()
                instance.stop()


if __name__ == "__main__":
    unittest.main()
