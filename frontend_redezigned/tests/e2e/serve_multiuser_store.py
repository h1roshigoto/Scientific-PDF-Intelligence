"""Run FastAPI and a separate worker for the synthetic two-account scenario."""

import os
import subprocess
import sys
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory

import uvicorn
from fastapi.responses import FileResponse

from serve_test_store import processing_fixture, rag_fixture, seed


def make_invitations():
    email = "operator@example.test"
    password = token_urlsafe(24)

    def cli(*args):
        result = subprocess.run([sys.executable, "-m", "spi.auth_cli", *args, "--password-stdin"],
                                input=password + "\n", text=True, capture_output=True, check=True, timeout=20)
        return result.stdout.strip()

    cli("bootstrap-admin", "--email", email, "--display-name", "E2E administrator")
    return [cli("invite", "--admin-email", email) for _ in range(2)]


if __name__ == "__main__":
    with TemporaryDirectory(prefix="spi-playwright-multiuser-") as folder:
        root = Path(folder)
        official = seed(root)
        os.environ.update(
            SPI_AUTH_MODE="multiuser", SPI_AUTH_LOCAL_DEVELOPMENT="true",
            SPI_AUTH_COOKIE_SECURE="false", SPI_AUTH_SECRET=token_urlsafe(32),
            SPI_AUTH_ALLOWED_ORIGINS="http://127.0.0.1:4178,http://127.0.0.1:8183",
            SPI_AUTH_TRUSTED_PROXIES="", SPI_AUTH_CSRF_ENABLED="true", SPI_RATE_LIMIT_ENABLED="true",
            SPI_PUBLIC_DEMO_ENABLED="false", SPI_AUTH_DB=str(root / "accounts" / "auth.sqlite3"),
            SPI_MULTIUSER_STORAGE_ROOT=str(root / "private"),
            SPI_STATE_DIR=str(official.root), SPI_UPLOAD_DIR="", SPI_UPLOAD_STATE_DIR="",
            SPI_UPLOAD_PROCESS_STATE_DIR="", SPI_UPLOAD_PROCESS_GIGACHAT="false",
        )
        from spi.api import app

        solar, rag = root / "synthetic-solar.pdf", root / "synthetic-rag.pdf"
        processing_fixture(solar)
        rag_fixture(rag)
        codes = make_invitations()

        @app.get("/__test/invitations", include_in_schema=False)
        def invitations():
            return {"alice": codes[0], "bob": codes[1]}

        @app.get("/__test/{domain}.pdf", include_in_schema=False)
        def fixture_pdf(domain: str):
            return FileResponse(solar if domain == "solar" else rag, media_type="application/pdf")

        with subprocess.Popen([sys.executable, "-m", "spi.upload_worker"]) as worker:
            try:
                uvicorn.run(app, host="127.0.0.1", port=8183, proxy_headers=False)
            finally:
                worker.terminate()
                worker.wait()
