"""Bounded Unix socket protocol for the Compose PDF service."""

import json
import os
import socket
import struct
from pathlib import Path

MAX_PDF_BYTES = 25 * 1024**2
MAX_RESPONSE_BYTES = 16 * 1024**2
MAX_PAGES = 2_000
TIMEOUT = 32


class ParserUnavailable(OSError):
    pass


def enabled():
    return bool(os.getenv("SPI_PDF_SOCKET"))


def receive(stream, maximum):
    header = stream.read(4)
    if len(header) != 4:
        raise ParserUnavailable("pdf_parser_protocol")
    size = struct.unpack("!I", header)[0]
    if size > maximum:
        raise ParserUnavailable("pdf_parser_size_limit")
    payload = stream.read(size)
    if len(payload) != size:
        raise ParserUnavailable("pdf_parser_protocol")
    return payload


def send(connection, payload):
    connection.sendall(struct.pack("!I", len(payload)))
    connection.sendall(payload)


def request(operation, content=b"", **parameters):
    if isinstance(content, Path):
        with content.open("rb") as source:
            content = source.read(MAX_PDF_BYTES + 1)
    if len(content) > MAX_PDF_BYTES:
        raise ParserUnavailable("pdf_parser_input_limit")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(TIMEOUT)
            connection.connect(os.environ["SPI_PDF_SOCKET"])
            send(connection, json.dumps({"op": operation, **parameters}).encode())
            send(connection, content)
            with connection.makefile("rb") as stream:
                result = json.loads(receive(stream, MAX_RESPONSE_BYTES))
    except (OSError, ValueError, RecursionError) as exc:
        raise ParserUnavailable("pdf_parser_unavailable") from exc
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise ParserUnavailable("pdf_parser_protocol")
    if result["status"] == "unavailable":
        raise ParserUnavailable("pdf_parser_unavailable")
    return result
