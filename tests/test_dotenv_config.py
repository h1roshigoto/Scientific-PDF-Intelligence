import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from spi.config import _PROJECT_ROOT


@pytest.fixture
def run_in_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(_PROJECT_ROOT / "src" / "spi", checkout / "src" / "spi",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(_PROJECT_ROOT / "pyproject.toml", checkout / "pyproject.toml")
    (checkout / ".env").write_text(
        "GIGACHAT_CREDENTIALS=fixtureKey==\n"
        "GIGACHAT_SCOPE=GIGACHAT_API_B2B\n"
        "SPI_STATE_DIR=dotenv-state\n"
        "SPI_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173\n",
        encoding="utf-8",
    )
    other_dir = tmp_path / "outside-checkout"
    other_dir.mkdir()
    (other_dir / ".env").write_text("GIGACHAT_CREDENTIALS=wrongFile\n", encoding="utf-8")
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("GIGACHAT_", "SPI_", "PYTHON_DOTENV_"))}
    env["PYTHONPATH"] = str(checkout / "src")

    def run(code, *args, **overrides):
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code), *args],
            cwd=other_dir, env=env | overrides, capture_output=True, text=True, check=True,
            timeout=30,
        )

    return run


@pytest.mark.parametrize("process_value", [None, "processKey==", ""])
def test_dotenv_credentials_and_process_priority(run_in_checkout, process_value):
    overrides = {} if process_value is None else {"GIGACHAT_CREDENTIALS": process_value}
    run_in_checkout(
        """
        import os
        import sys
        from spi.gigachat import GigaChatSettings

        settings = GigaChatSettings.from_env()
        assert settings.credentials.get_secret_value() == sys.argv[1]
        assert settings.scope == "GIGACHAT_API_B2B"
        assert os.getenv("GIGACHAT_CREDENTIALS") == sys.argv[1]
        """,
        "fixtureKey==" if process_value is None else process_value,
        **overrides,
    )


def test_missing_credentials_with_no_dotenv(run_in_checkout):
    run_in_checkout(
        """
        import httpx
        from spi.config import _PROJECT_ROOT
        from spi.gigachat import GigaChatClient, GigaChatError

        (_PROJECT_ROOT / ".env").unlink()

        def reject_network(_):
            raise AssertionError("unexpected HTTP request")

        try:
            GigaChatClient(transport=httpx.MockTransport(reject_network))
        except GigaChatError as error:
            assert error.code == "missing_credentials"
        else:
            raise AssertionError("expected a missing credentials error")
        """
    )


def test_oauth_secrets_stay_out_of_errors_and_logs(run_in_checkout):
    result = run_in_checkout(
        """
        import logging
        import time

        import httpx
        from spi.gigachat import GigaChatClient, GigaChatError

        def handle(request):
            if request.url.path.endswith("/oauth"):
                assert request.headers["Authorization"] == "Basic fixtureKey=="
                return httpx.Response(200, json={
                    "access_token": "fixtureOAuthToken",
                    "expires_at": int(time.time()) + 1800,
                })
            assert request.headers["Authorization"] == "Bearer fixtureOAuthToken"
            return httpx.Response(401, text="fixtureKey== fixtureOAuthToken")

        logging.basicConfig(level=logging.INFO)
        with GigaChatClient(transport=httpx.MockTransport(handle)) as client:
            try:
                client.list_models()
            except GigaChatError as error:
                assert error.status == 401
                print(error)
            else:
                raise AssertionError("expected an HTTP 401 error")
        """
    )
    output = result.stdout + result.stderr
    assert "HTTP 401" in output
    assert "fixtureKey==" not in output
    assert "fixtureOAuthToken" not in output


@pytest.mark.parametrize(("module", "args", "exit_code"), [
    ("spi.gigachat_smoke", ["--prepare-only"], 1),
    ("spi.gigachat_pipeline", [], 1),
    ("spi.cli", ["--domain", "solar", "--manifest"], 0),
])
def test_cli_loads_dotenv_when_started(run_in_checkout, module, args, exit_code):
    result = run_in_checkout(
        """
        import os
        import runpy
        import sys

        exit_code = int(sys.argv.pop(1))
        module = sys.argv.pop(1)
        try:
            runpy.run_module(module, run_name="__main__")
        except SystemExit as error:
            assert error.code == exit_code
        assert os.getenv("GIGACHAT_CREDENTIALS") == "fixtureKey=="
        """,
        str(exit_code), module, *args,
    )
    assert "fixtureKey==" not in result.stdout + result.stderr


def test_fastapi_loads_state_and_cors(run_in_checkout):
    run_in_checkout(
        """
        from pathlib import Path

        from fastapi.testclient import TestClient
        from spi.api import app
        from spi.storage import Store

        documents = [{"document_id": "solar:D001", "domain": "solar"}]
        Store(Path("dotenv-state")).write("documents.json", documents)
        client = TestClient(app)
        assert client.get("/documents").json() == documents
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            response = client.get("/health", headers={"Origin": origin})
            assert response.json() == {"status": "ok"}
            assert response.headers["access-control-allow-origin"] == origin
        """
    )


def test_local_dotenv_is_ignored_and_untracked():
    if not (_PROJECT_ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("requires a Git checkout")
    ignored = subprocess.run(
        ["git", "check-ignore", "--", ".env", ".env.local", ".env.production", ".env.example"],
        cwd=_PROJECT_ROOT, check=True, capture_output=True, text=True,
    )
    assert ignored.stdout.splitlines() == [".env", ".env.local", ".env.production"]
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".env", ".env.local", ".env.*", ":!.env.example"],
        cwd=_PROJECT_ROOT, check=True, capture_output=True, text=True,
    )
    assert not tracked.stdout
