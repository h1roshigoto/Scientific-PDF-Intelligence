"""Bounded Unix socket protocol for the Compose PDF service."""

import json
import os
import socket
import struct
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

MAX_PDF_BYTES = 25 * 1024**2
MAX_RESPONSE_BYTES = 16 * 1024**2
MAX_PAGES = 2_000
TIMEOUT = 32
MAX_ARGUMENT_BYTES = 256


class ParserUnavailable(OSError):
    pass


def enabled():
    return (bool(os.getenv("SPI_PDF_SOCKET")) or os.getenv("SPI_DEPLOYMENT_PROFILE") == "production"
            or os.getenv("SPI_DEPLOYMENT", "").strip().lower() in {"1", "true"})


PageNumber = Annotated[int, Field(ge=1, le=MAX_PAGES)]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SimpleRequest(Message):
    op: Literal["ping", "pages"]


class EvidenceRequest(Message):
    op: Literal["evidence"]
    page: PageNumber


class LayoutRequest(Message):
    op: Literal["layout"]
    domain: Literal["solar", "rag"]


REQUEST = TypeAdapter(Annotated[SimpleRequest | EvidenceRequest | LayoutRequest, Field(discriminator="op")])


class Failure(Message):
    status: Literal["invalid", "encrypted", "unavailable"]


class Ping(Message):
    status: Literal["ok"]


class Pages(Ping):
    pages: PageNumber


class Evidence(Pages):
    has_text: bool


def decode_json(payload):
    def unique_pairs(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ValueError("duplicate protocol field")
            record[key] = value
        return record

    return json.loads(payload.decode("utf-8"), object_pairs_hook=unique_pairs)


def _remaining(deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("pdf_parser_deadline")
    return remaining


def _exact(connection, size, deadline):
    buffer = bytearray(size)
    view = memoryview(buffer)
    while view:
        connection.settimeout(_remaining(deadline))
        count = connection.recv_into(view)
        if not count:
            raise ParserUnavailable("pdf_parser_protocol")
        view = view[count:]
    return buffer


def receive(connection, maximum, deadline):
    header = _exact(connection, 4, deadline)
    size = struct.unpack("!I", header)[0]
    if size > maximum:
        raise ParserUnavailable("pdf_parser_size_limit")
    return _exact(connection, size, deadline)


def send(connection, payload, deadline):
    for part in (struct.pack("!I", len(payload)), payload):
        connection.settimeout(_remaining(deadline))
        connection.sendall(part)


def request(operation, content=b"", *, validate: Callable | None = None, timeout=TIMEOUT, **parameters):
    if isinstance(content, Path):
        with content.open("rb") as source:
            content = source.read(MAX_PDF_BYTES + 1)
    if len(content) > MAX_PDF_BYTES:
        raise ParserUnavailable("pdf_parser_input_limit")
    try:
        arguments = REQUEST.validate_python({"op": operation, **parameters})
        if (operation == "ping") != (len(content) == 0):
            raise ValueError("invalid parser input")
        socket_path = os.getenv("SPI_PDF_SOCKET")
        if not socket_path:
            raise ParserUnavailable("pdf_parser_not_configured")
        deadline = monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_remaining(deadline))
            connection.connect(socket_path)
            send(connection, arguments.model_dump_json().encode(), deadline)
            send(connection, content, deadline)
            result = decode_json(receive(connection, MAX_RESPONSE_BYTES, deadline))
        if not isinstance(result, dict):
            raise ValueError("invalid parser response")
        if result.get("status") != "ok":
            failure = Failure.model_validate(result)
            if failure.status == "unavailable" or operation == "ping":
                raise ParserUnavailable("pdf_parser_unavailable")
            return failure.model_dump()
        if validate is not None and operation == "layout":
            return validate(result)
        schema = {"ping": Ping, "pages": Pages, "evidence": Evidence}[operation]
        return schema.model_validate(result).model_dump()
    except (OSError, ValueError, KeyError, RecursionError) as exc:
        raise ParserUnavailable("pdf_parser_unavailable") from exc
