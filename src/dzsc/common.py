from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def detect_newline(data: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in data else b"\n"


def restore_file(path: Path, backup: Path | None) -> None:
    if backup is not None and backup.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, path)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def make_user_executable(path: Path) -> None:
    try:
        current = path.stat().st_mode
        path.chmod(current | stat.S_IXUSR)
    except OSError:
        pass


def ensure_project_dir(project_dir: Path) -> None:
    if not project_dir.is_dir():
        raise SystemExit(f"project directory not found: {project_dir}")


def resolve_gradle_wrapper(project_dir: Path) -> list[str]:
    gradlew_bat = project_dir / "gradlew.bat"
    gradlew = project_dir / "gradlew"

    if os.name == "nt":
        if gradlew_bat.exists():
            return ["cmd", "/c", str(gradlew_bat)]
        if gradlew.exists():
            return ["cmd", "/c", str(gradlew)]
        raise SystemExit(f"gradle wrapper not found: {gradlew_bat} or {gradlew}")

    if not gradlew.exists():
        raise SystemExit(f"gradlew not found: {gradlew}")
    if not os.access(gradlew, os.X_OK):
        raise SystemExit(f"gradlew not executable: {gradlew}")
    return [str(gradlew)]


def gradle_command(project_dir: Path, *args: str) -> list[str]:
    extra_args = shlex.split(os.environ.get("DZSC_GRADLE_ARGS", ""))
    return [*resolve_gradle_wrapper(project_dir), *extra_args, *args]


def run_gradle_task(project_dir: Path, task: str, *, env: dict[str, str] | None = None) -> None:
    cmd = gradle_command(project_dir, task)
    print("running:", " ".join(cmd))
    subprocess.run(cmd, cwd=project_dir, env=env, check=True)


def default_python_bin() -> str:
    return sys.executable or "python3"
