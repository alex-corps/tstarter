"""Точка входа контейнера: готовит базу и запускает панель."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.check_call([sys.executable, str(ROOT / "scripts" / "bootstrap_db.py")], cwd=ROOT)
    raise SystemExit(subprocess.call([
        sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
        "--server.address=0.0.0.0",
        "--server.port=8501",
        "--server.headless=true",
        "--server.fileWatcherType=none",
    ], cwd=ROOT))


if __name__ == "__main__":
    main()
