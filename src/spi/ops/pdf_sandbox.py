"""Serial, networkless PDF service. Only the request document reaches MuPDF."""

import argparse
import ctypes
import json
import os
import socketserver
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from spi import pdf_boundary

SOCKET = Path("/parser/pdf.sock")
CHILD_TIMEOUT = 20


def restrict_child():
    """Allow reads and inherited file descriptors under seccomp and Landlock."""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (pdf_boundary.MAX_RESPONSE_BYTES, pdf_boundary.MAX_RESPONSE_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    seccomp = ctypes.CDLL("libseccomp.so.2")
    # Landlock ABI 3 handles truncation as well as filesystem creation and writes.
    if libc.syscall(444, 0, 0, 1) < 3:
        raise RuntimeError("landlock_abi_unavailable")
    write_access = ctypes.c_uint64(((1 << 15) - 1) & ~((1 << 0) | (1 << 2) | (1 << 3)))
    fd = libc.syscall(444, ctypes.byref(write_access), ctypes.sizeof(write_access), 0)
    if fd < 0:
        raise RuntimeError("landlock_create")
    try:
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
            raise RuntimeError("landlock_restrict")
    finally:
        os.close(fd)

    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    policy = seccomp.seccomp_init(0x00050001)  # EPERM for omitted syscalls.
    if not policy:
        raise RuntimeError("seccomp_create")
    try:
        allowed = """
            read write readv writev close lseek pread64 fstat newfstatat stat statx lstat
            open openat readlink readlinkat faccessat faccessat2 access getdents64 fcntl ioctl
            mmap mprotect munmap mremap brk madvise futex set_robust_list rseq arch_prctl
            rt_sigaction rt_sigprocmask rt_sigreturn sigaltstack
            getpid gettid getuid geteuid getgid getegid getcwd uname sched_getaffinity
            clock_gettime gettimeofday getrandom prlimit64 exit exit_group
        """
        for name in allowed.split():
            syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
            if syscall >= 0 and seccomp.seccomp_rule_add(policy, 0x7FFF0000, syscall, 0):
                raise RuntimeError("seccomp_rule")
        if seccomp.seccomp_load(policy):
            raise RuntimeError("seccomp_load")
    finally:
        seccomp.seccomp_release(policy)


def parse(operation, value, path):
    import pymupdf

    from spi.pdf_layout import read_layout

    restrict_child()
    if operation == "ping":
        return {"status": "ok"}
    try:
        with pymupdf.open(path, filetype="pdf") as document:
            if not document.is_pdf or document.is_repaired or not 1 <= document.page_count <= pdf_boundary.MAX_PAGES:
                return {"status": "invalid"}
            if document.needs_pass or document.xref_get_key(-1, "Encrypt")[0] != "null":
                return {"status": "encrypted"}
            pages = document.page_count
            if operation == "pages":
                return {"status": "ok", "pages": pages}
            if operation == "evidence":
                number = int(value)
                has_text = 1 <= number <= pages and bool(document[number - 1].get_text().strip())
                return {"status": "ok", "pages": pages, "has_text": has_text}
        _, pages, fragments, issues = read_layout(path, value)
        return {"status": "ok", "pages": pages, "fragments": [asdict(item) for item in fragments], "issues": issues}
    except (RuntimeError, ValueError, IndexError, OSError):
        return {"status": "invalid"}


class _Request(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            arguments = json.loads(pdf_boundary.receive(self.rfile, 256))
            content = pdf_boundary.receive(self.rfile, pdf_boundary.MAX_PDF_BYTES)
            operation = arguments["op"]
            value = ""
            if operation == "layout" and arguments.get("domain") in {"solar", "rag"}:
                value = arguments["domain"]
            elif operation == "evidence" and type(arguments.get("page")) is int:
                value = str(arguments["page"])
            elif operation not in {"pages", "ping"}:
                raise ValueError("invalid parser operation")
            if operation == "ping" and content:
                raise ValueError("unexpected parser input")
            with TemporaryDirectory() as temporary:
                path = Path(temporary) / "document.pdf"
                path.write_bytes(content)
                with (Path(temporary) / "result").open("w+b") as output:
                    subprocess.run(
                        [sys.executable, "-m", __spec__.name, "child", operation, value, str(path), str(output.fileno())],
                        check=True, timeout=CHILD_TIMEOUT, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(output.fileno(),),
                        env={key: os.environ[key] for key in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE")},
                    )
                    output.seek(0)
                    result = output.read(pdf_boundary.MAX_RESPONSE_BYTES + 1)
                    if not result or len(result) > pdf_boundary.MAX_RESPONSE_BYTES:
                        raise ValueError("invalid parser result")
        except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.SubprocessError):
            result = b'{"status":"unavailable"}'
        try:
            pdf_boundary.send(self.request, result)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "check", "child"))
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    if args.action == "child":
        operation, value, path, output_fd = args.arguments
        result = parse(operation, value, path)
        # Native parser diagnostics can write to stdout. Keep the framed
        # response on a separate descriptor and discard both diagnostic streams.
        with os.fdopen(int(output_fd), "w", encoding="utf-8") as output:
            json.dump(result, output, allow_nan=False, separators=(",", ":"))
    elif args.action == "check":
        os.environ["SPI_PDF_SOCKET"] = str(SOCKET)
        if pdf_boundary.request("ping") != {"status": "ok"}:
            raise SystemExit(1)
    else:
        os.umask(0o077)
        SOCKET.unlink(missing_ok=True)
        with socketserver.UnixStreamServer(str(SOCKET), _Request) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
