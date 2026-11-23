"""Run inside the disposable PDF sandbox container to exercise its Unix socket."""

import json
import socket
import struct
from time import monotonic

import pymupdf

from spi import pdf_boundary
from spi.ops.pdf_sandbox import SOCKET


def frame(payload):
    return struct.pack("!I", len(payload)) + payload


def connection():
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(8)
    client.connect(str(SOCKET))
    return client


def exchange(*parts):
    with connection() as client:
        for payload in parts:
            client.sendall(payload)
        return json.loads(pdf_boundary.receive(client, 256, monotonic() + 7))


ERROR = {"status": "unavailable"}


def main():
    for invalid in (
        b"{", b'{"op":"pages","path":"/should-not-be-used"}', b'{"op":"unsupported"}',
        b'{"op":"evidence","page":true}', b'{"op":"evidence","page":0}',
        b'{"op":"layout","domain":"other"}', b'{"op":"pages","op":"ping"}',
    ):
        assert exchange(frame(invalid)) == ERROR
    assert exchange(frame(b'{"op":"pages"}'), struct.pack("!I", pdf_boundary.MAX_PDF_BYTES + 1)) == ERROR
    assert exchange(frame(b'{"op":"ping"}'), frame(b"x")) == ERROR
    assert exchange(*[bytes([byte]) for byte in frame(b'{"op":"ping"}') + frame(b"")]) == {"status": "ok"}
    assert exchange(frame(b'{"op":"pages"}'), frame(b"%PDF-1.7\ninvalid")) == {"status": "invalid"}
    with pymupdf.open() as document:
        for _ in range(pdf_boundary.MAX_PAGES + 1):
            document.new_page()
        assert exchange(frame(b'{"op":"pages"}'), frame(document.tobytes())) == {"status": "invalid"}
    with connection() as client:
        client.sendall(b"\x00\x00")
        client.shutdown(socket.SHUT_WR)
        assert json.loads(pdf_boundary.receive(client, 256, monotonic() + 7)) == ERROR
    started = monotonic()
    assert exchange(b"\x00") == ERROR
    assert 4 <= monotonic() - started < 8
    assert exchange(frame(b'{"op":"ping"}'), frame(b"")) == {"status": "ok"}
    with connection() as client:
        client.sendall(b"\x00\x00")
    assert exchange(frame(b'{"op":"ping"}'), frame(b"")) == {"status": "ok"}
    print("PASS actual Unix socket strict schema, split frames, PDF and page bounds, deadline and disconnect recovery")


if __name__ == "__main__":
    main()
