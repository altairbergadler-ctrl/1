"""Minimal HTTPS CONNECT proxy restricted to the exact Qobuz/Akamai hosts."""

from __future__ import annotations

import select
import socket
import socketserver

ALLOWED = frozenset(
    {
        "play.qobuz.com",
        "open.qobuz.com",
        "www.qobuz.com",
        "static.qobuz.com",
        "streaming-qobuz-std.akamaized.net",
        "streaming-qobuz-sec.akamaized.net",
    }
)
MAX_HEADER_LINES = 64


class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(15)
        raw_request_line = self.rfile.readline(4097)
        if len(raw_request_line) > 4096 or not raw_request_line.endswith(b"\n"):
            self.wfile.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
            return
        request_line = raw_request_line.decode("ascii", "replace").strip()
        parts = request_line.split()
        for _ in range(MAX_HEADER_LINES):
            line = self.rfile.readline(4097)
            if len(line) > 4096 or (line and not line.endswith(b"\n")):
                self.wfile.write(
                    b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n"
                )
                return
            if line in (b"\r\n", b"\n", b""):
                break
        else:
            self.wfile.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
            return
        if len(parts) != 3 or parts[0] != "CONNECT" or ":" not in parts[1]:
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, port_text = parts[1].rsplit(":", 1)
        host = host.casefold().rstrip(".")
        if host not in ALLOWED or port_text != "443":
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            upstream = socket.create_connection((host, 443), timeout=15)
        except OSError:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        with upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            sockets = [self.connection, upstream]
            while True:
                readable, _, errored = select.select(sockets, [], sockets, 30)
                if errored or not readable:
                    return
                for source in readable:
                    try:
                        data = source.recv(64 * 1024)
                    except OSError:
                        return
                    if not data:
                        return
                    target = upstream if source is self.connection else self.connection
                    target.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16


if __name__ == "__main__":
    with Server(("0.0.0.0", 3128), Proxy) as server:
        server.serve_forever()
