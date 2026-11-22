"""Run the built site, FastAPI and the local PDF worker on loopback."""

import argparse
import getpass
import os
import re
import secrets
import ssl
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend_redezigned"
HOST = "127.0.0.1"


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def check_processes(running: list[tuple[str, subprocess.Popen]]) -> None:
    for name, process in running:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited with code {process.returncode}.")


def wait_until_ready(url: str, running: list[tuple[str, subprocess.Popen]]) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = monotonic() + 15
    while monotonic() < deadline:
        check_processes(running)
        try:
            with opener.open(url, timeout=1):
                return
        except OSError:
            sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    app_data = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".local" / "share")
    parser.add_argument("--data-dir", type=Path, default=app_data / "ScientificPDFIntelligence" / "demo-upload")
    parser.add_argument("--port", type=int, default=5173, help="Site port (default: 5173)")
    parser.add_argument("--api-port", type=int, default=8000, help="FastAPI port (default: 8000)")
    parser.add_argument("--cloudflare-access", action="store_true", help="Require a signed Cloudflare Access user session")
    parser.add_argument("--gigachat", action="store_true", help="Opt into model enrichment of selected PDF text")
    args = parser.parse_args()
    if args.port == args.api_port or not all(1 <= port <= 65535 for port in (args.port, args.api_port)):
        parser.error("Choose two different ports between 1 and 65535.")
    data = args.data_dir.resolve()
    if data.is_relative_to(ROOT) or ROOT.is_relative_to(data):
        parser.error("Use a dedicated demo data directory outside the checkout.")
    node = shutil.which("node")
    vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if node is None or not vite.is_file() or not (FRONTEND / "dist" / "index.html").is_file():
        parser.error("Install frontend dependencies and run pnpm build first.")

    if args.cloudflare_access:
        os.environ["SPI_DEMO_ACCESS"] = "true"
        try:
            from spi.demo_access import DemoAccess

            access = DemoAccess()
        except (ImportError, ValueError) as exc:
            parser.error(f"Configure Cloudflare Access and install the demo-access extra ({type(exc).__name__}).")
    use_model = args.gigachat
    if use_model:
        bundle = os.getenv("GIGACHAT_CA_BUNDLE_FILE")
        try:
            if not os.getenv("GIGACHAT_CREDENTIALS", "").strip() or not bundle:
                raise ValueError
            ssl.create_default_context(cafile=bundle)
        except (OSError, ValueError):
            print("GigaChat credentials or trusted CA bundle unavailable. Starting local analysis.", file=sys.stderr)
            use_model = False
    token = secrets.token_urlsafe(32) if args.cloudflare_access else os.getenv("SPI_UPLOAD_TOKEN") or getpass.getpass("Private demo token (at least 32 URL-safe characters): ")
    if len(token) < 32 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,2}", token):
        parser.error("Provide a token with at least 32 URL-safe characters.")

    backend = f"http://{HOST}:{args.api_port}"
    backend_env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), SPI_AUTH_MODE="private",
                       SPI_DEPLOYMENT="false", SPI_PDF_SOCKET="", SPI_WORKER_JOB_TIMEOUT="0",
                       SPI_STATE_DIR=str(data / "registry"), SPI_UPLOAD_ENABLED="true", SPI_UPLOAD_TOKEN=token,
                       SPI_UPLOAD_DIR=str(data / "pdfs"), SPI_UPLOAD_STATE_DIR=str(data / "metadata"),
                       SPI_UPLOAD_PROCESS_STATE_DIR=str(data / "processing"), SPI_UPLOAD_MAX_BYTES="26214400",
                       SPI_UPLOAD_MAX_FILES="100", SPI_UPLOAD_PROCESS_GIGACHAT=str(use_model).lower(),
                       SPI_DEMO_ACCESS=str(args.cloudflare_access).lower(), SPI_DEMO_MODEL_FALLBACK="true",
                       SPI_UPLOAD_PROCESS_MAX_MODEL_REQUESTS="8")
    frontend_env = dict(os.environ, SPI_BACKEND_URL=backend)
    for key in ("SPI_UPLOAD_TOKEN", "GIGACHAT_CREDENTIALS"):
        frontend_env.pop(key, None)
    if args.cloudflare_access:
        frontend_env["__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"] = access.host
    services = (
        ("FastAPI", [sys.executable, "-m", "uvicorn", "spi.api:app", "--host", HOST, "--port", str(args.api_port)],
         ROOT, backend_env),
        ("Worker", [sys.executable, "-m", "spi.upload_worker"], ROOT, backend_env),
        ("Site", [node, str(vite), "preview", "--host", HOST, "--port", str(args.port), "--strictPort"],
         FRONTEND, frontend_env),
    )
    site = f"http://{HOST}:{args.port}/"
    try:
        with ExitStack() as processes:
            running = []
            for name, command, cwd, env in services:
                if name == "Site":
                    wait_until_ready(f"{backend}/health", running)
                process = subprocess.Popen(command, cwd=cwd, env=env)
                processes.callback(stop, process)
                running.append((name, process))
            wait_until_ready(site, running)
            print(f"Private demo ready at {site} (Ctrl+C to stop)", flush=True)
            while True:
                check_processes(running)
                sleep(0.25)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
