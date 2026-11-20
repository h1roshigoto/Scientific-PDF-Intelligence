"""Load local configuration alongside the project sources."""

from pathlib import Path

from dotenv import load_dotenv


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_project_env() -> None:
    if (_PROJECT_ROOT / "pyproject.toml").is_file():
        load_dotenv(_PROJECT_ROOT / ".env", override=False, encoding="utf-8-sig")
