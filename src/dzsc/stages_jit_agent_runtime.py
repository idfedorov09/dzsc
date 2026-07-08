from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

from dzsc.common import detect_newline, ensure_project_dir, gradle_command, rmdir_if_empty
from dzsc.payloads import copy_payload_tree, payload_text
from dzsc.sdk import StageRunContext, stage


BUILD_MARKER_START = "// >>> dzsc-jit-agent-runtime (managed)"
BUILD_MARKER_END = "// <<< dzsc-jit-agent-runtime (managed)"
PAYLOAD_STATIC_TOKEN = "__DZSC_AGENT_RUNTIME_STATIC_DIR__"


def _managed_build_block(newline: bytes, apply_rel: str) -> bytes:
    return (
        newline
        + BUILD_MARKER_START.encode("utf-8")
        + newline
        + f"apply from: '{apply_rel}'".encode("utf-8")
        + newline
        + BUILD_MARKER_END.encode("utf-8")
        + newline
    )


def _remove_managed_build_block(build_data: bytes) -> bytes:
    start = BUILD_MARKER_START.encode("utf-8")
    end = BUILD_MARKER_END.encode("utf-8")
    if start not in build_data and end not in build_data:
        return build_data

    pattern = re.compile(
        rb"(?:\r?\n)?"
        + re.escape(start)
        + rb"\r?\n"
        + rb"[ \t]*apply from: '[^']+'\r?\n"
        + re.escape(end)
        + rb"(?:\r?\n)?",
        re.M,
    )
    patched, count = pattern.subn(b"", build_data, count=1)
    if count == 0:
        raise SystemExit("stale jit agent runtime managed markers found in build.gradle, but block is malformed")
    print("cleaned stale managed jit agent runtime hook from build.gradle")
    return patched


def _insert_managed_build_block(build_data: bytes, apply_rel: str) -> bytes:
    newline = detect_newline(build_data)
    block = _managed_build_block(newline, apply_rel)
    anchor = re.compile(rb"(?m)^apply from: ['\"]docker\.gradle['\"]\r?$$")
    matches = list(anchor.finditer(build_data))
    if matches:
        idx = matches[-1].start()
        return build_data[:idx] + block + build_data[idx:]
    suffix = b"" if build_data.endswith((b"\n", b"\r")) else newline
    return build_data + suffix + block


def _run_gradle(project_dir: Path, *args: str) -> None:
    cmd = gradle_command(project_dir, *args)
    print("running:", " ".join(cmd))
    subprocess.run(cmd, cwd=project_dir, check=True)


def inject_jit_agent_runtime(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    build_gradle = project_dir / "build.gradle"
    if not build_gradle.is_file():
        raise SystemExit(f"build.gradle not found: {build_gradle}")

    build_baseline = _remove_managed_build_block(build_gradle.read_bytes())
    payload = payload_text("jit_agent_runtime_gradle")
    if PAYLOAD_STATIC_TOKEN not in payload:
        raise SystemExit(f"invalid jit agent runtime payload: {PAYLOAD_STATIC_TOKEN} marker is missing")

    run_dir = project_dir / ".dzsc" / "run" / f"jit-agent-runtime-{uuid.uuid4().hex[:8]}"
    payload_target = run_dir / "gradle" / "jit-agent-runtime.gradle"
    static_target = run_dir / "agent-runtime"

    try:
        payload_target.parent.mkdir(parents=True, exist_ok=True)
        copy_payload_tree("agent-runtime", static_target)

        static_rel = static_target.relative_to(project_dir).as_posix()
        payload_target.write_text(payload.replace(PAYLOAD_STATIC_TOKEN, static_rel), encoding="utf-8")

        apply_rel = payload_target.relative_to(project_dir).as_posix()
        build_gradle.write_bytes(_insert_managed_build_block(build_baseline, apply_rel))

        _run_gradle(project_dir, "--no-daemon", "dzscAgentRuntimeInstall", "-x", "assembleWeb")
        return 0
    finally:
        build_gradle.write_bytes(build_baseline)
        shutil.rmtree(run_dir, ignore_errors=True)
        rmdir_if_empty(project_dir / ".dzsc" / "run")
        rmdir_if_empty(project_dir / ".dzsc")


def jit_agent_runtime_status(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    build_gradle = project_dir / "build.gradle"
    install_dir = project_dir / "target" / "install" / project_dir.name
    deps_dir = project_dir / "target" / "web" / "WEB-INF" / "dzsc-agent" / "dependencies"
    runner = project_dir / "target" / "bin" / "dzsc-agent-runtime-run"
    installed_runner = install_dir / "bin" / "dzsc-agent-runtime-run"

    build_data = build_gradle.read_bytes() if build_gradle.exists() else b""
    print(
        f"Root build hook (should be absent after one-shot): "
        f"{'PRESENT' if BUILD_MARKER_START.encode('utf-8') in build_data else 'ABSENT'}"
    )
    print(f"agent deps dir: {'PRESENT' if deps_dir.is_dir() else 'MISSING'}")
    if deps_dir.is_dir():
        for item in sorted(deps_dir.iterdir()):
            if item.is_file():
                print(f"  - {item.name}")
    print(f"build runner: {'PRESENT' if runner.is_file() else 'MISSING'}")
    print(f"installed runner: {'PRESENT' if installed_runner.is_file() else 'MISSING'}")
    return 0


INJECT_JIT_AGENT_RUNTIME_STAGE = stage(
    "inject_jit_agent_runtime",
    "Build/install Doczilla server-side JIT bridge for agent runtime BL execution",
    aliases=("inject-jit-agent-runtime", "jit_agent_runtime", "jit-agent-runtime"),
)(inject_jit_agent_runtime)

JIT_AGENT_RUNTIME_STATUS_STAGE = stage(
    "jit_agent_runtime_status",
    "Show server-side JIT agent runtime build artifact status",
    aliases=("jit-agent-runtime-status",),
)(jit_agent_runtime_status)
