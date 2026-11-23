# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.
""" The ePays API as Odoo's container sees it: `localhost:7124` forwarded to the local ePays API on
the host, so that the addon's fixed Localhost URL works unchanged; and an HTTPS proxy on port 3128,
through which a shop on the ePays sandbox calls it.

TLS passes through untouched. While the shop's flag file exists, the answer to a request sent on a
new connection is held for `DELAY` seconds, longer than the addon's payment request timeout: ePays
receives and runs the request, and Odoo times out, as with a slow server.
"""

import contextlib
import os
import socket
import sys
import threading
import time

LISTEN = ('127.0.0.1', 7124)
TARGET = ('host.docker.internal', 7124)
PROXY = ('127.0.0.1', 3128)
DELAY = 40
TLS_APPLICATION_DATA = 23


class Hold:
    """ When to release the server's bytes of one connection.

    Only what the server sends after the client's first encrypted record (TLS content type 23: its
    Finished in TLS 1.3, its request in TLS 1.2) is held, so the handshake completes and the request
    reaches ePays.
    """

    def __init__(self, active):
        self.active = active
        self.until = 0.0
        self.buffer = b''

    def client_sent(self, data):
        if not self.active or self.until:
            return
        self.buffer += data
        while len(self.buffer) >= 5:
            content_type, length = self.buffer[0], int.from_bytes(self.buffer[3:5], 'big')
            if content_type == TLS_APPLICATION_DATA:
                self.until = time.time() + DELAY
                print(f"request sent: holding the answer for {DELAY}s", flush=True)
                return
            if len(self.buffer) < 5 + length:
                return
            self.buffer = self.buffer[5 + length:]

    def wait(self):
        remaining = self.until - time.time()
        if remaining > 0:
            time.sleep(remaining)


def pipe(source, target, hold, from_server):
    try:
        while data := source.recv(65536):
            if from_server:
                hold.wait()
            else:
                hold.client_sent(data)
            target.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            source.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            target.shutdown(socket.SHUT_RDWR)


def tunnel(client, upstream, flag):
    hold = Hold(os.path.exists(flag))
    threading.Thread(target=pipe, args=(client, upstream, hold, False), daemon=True).start()
    threading.Thread(target=pipe, args=(upstream, client, hold, True), daemon=True).start()


def handle_forward(client, flag):
    tunnel(client, socket.create_connection(TARGET), flag)


def handle_proxy(client, flag):
    """ An HTTPS proxy (`CONNECT host:port`), for a shop that calls the ePays sandbox: Odoo reaches
    it through `HTTPS_PROXY`, and its answers can be held like the local API's. """
    request = b''
    while b'\r\n\r\n' not in request:
        data = client.recv(4096)
        if not data:
            client.close()
            return
        request += data
    method, address = request.split(b' ', 2)[:2]
    if method != b'CONNECT':
        client.sendall(b'HTTP/1.1 405 Method Not Allowed\r\n\r\n')
        client.close()
        return
    host, port = address.decode().rsplit(':', 1)
    upstream = socket.create_connection((host, int(port)))
    client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
    tunnel(client, upstream, flag)


def serve(address, handler, flag):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(address)
    server.listen(64)
    while True:
        connection, _address = server.accept()
        threading.Thread(target=handler, args=(connection, flag), daemon=True).start()


def main():
    flag = sys.argv[1]
    print(f"forwarding {LISTEN} -> {TARGET} and proxying HTTPS on {PROXY}, holding answers while "
          f"{flag} exists", flush=True)
    threading.Thread(target=serve, args=(PROXY, handle_proxy, flag), daemon=True).start()
    serve(LISTEN, handle_forward, flag)


if __name__ == '__main__':
    main()
