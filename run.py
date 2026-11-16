"""One-command launcher: sets up a local virtualenv on first run, then starts the dashboard.

Needs only Python 3.10+ (the web UI ships prebuilt in frontend/dist, so Node is not needed).

    python run.py                  start the dashboard (opens http://127.0.0.1:8000)
    python run.py --port 9000      any start.py flag is passed through
    python run.py scrape [flags]   run one scrape from the terminal (scrape.py flags)
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".requirements.sha256"


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def fail(message: str) -> None:
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(1)


def create_venv() -> None:
    print(f"Creating virtual environment in {VENV} ...")
    # A subprocess, because Debian's patched venv calls sys.exit() when ensurepip is missing.
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV)], capture_output=True, text=True
    )
    if result.returncode == 0:
        return
    shutil.rmtree(VENV, ignore_errors=True)
    uv = shutil.which("uv")
    if uv:
        print("Python's venv module is unavailable here; using uv instead.")
        subprocess.check_call([uv, "venv", "--python", sys.executable, str(VENV)])
        return
    hint = (
        "On Debian/Ubuntu install it with:  sudo apt install python3-venv"
        if sys.platform.startswith("linux")
        else "Reinstall Python from https://www.python.org/downloads/"
    )
    details = (result.stderr or result.stdout).strip()
    fail(f"could not create a virtual environment.\n{details}\n\n{hint}")


def has_pip(python: Path) -> bool:
    probe = [str(python), "-m", "pip", "--version"]
    return subprocess.run(probe, capture_output=True).returncode == 0


def install_requirements() -> None:
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if STAMP.exists() and STAMP.read_text().strip() == digest:
        return
    python = venv_python()
    print("Installing dependencies (first run only, takes a minute) ...")
    if has_pip(python):
        cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    elif shutil.which("uv"):
        cmd = [shutil.which("uv") or "uv", "pip", "install", "--python", str(python)]
    else:
        if subprocess.run([str(python), "-m", "ensurepip", "--upgrade"]).returncode:
            fail(f"pip is not available in {VENV}; delete that folder and run again.")
        cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    try:
        subprocess.check_call([*cmd, "-r", str(REQUIREMENTS)])
    except subprocess.CalledProcessError:
        fail("dependency installation failed (check your internet connection).")
    STAMP.write_text(digest)


def main() -> None:
    if sys.version_info < (3, 10):
        fail(f"Python 3.10+ is required, this is {sys.version.split()[0]}.")
    if not venv_python().exists():
        create_venv()
    install_requirements()
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        print("Note: frontend/dist is missing; build it with: cd frontend && npm install && npm run build")

    args = sys.argv[1:]
    script = "start.py"
    if args and args[0] == "scrape":
        script, args = "scrape.py", args[1:]
    cmd = [str(venv_python()), str(ROOT / script), *args]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=os.environ.copy())
    while True:
        try:
            sys.exit(proc.wait())
        except KeyboardInterrupt:
            continue  # the child got Ctrl+C too; wait for it to shut down cleanly


if __name__ == "__main__":
    main()
