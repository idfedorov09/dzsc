from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from dzsc.common import detect_newline, ensure_project_dir, rmdir_if_empty, run_gradle_task
from dzsc.payloads import payload_text
from dzsc.sdk import StageRunContext, stage


BUILD_MARKER_START = "// >>> dzsc-agentation-debug (managed)"
BUILD_MARKER_END = "// <<< dzsc-agentation-debug (managed)"
PROJECT_AGENTATION_HOOK_MARKER = "AGENTATION_GRADLE_HOOK_BEGIN"

DEBUG_MARKER_BEGIN = "AGENTATION_DEBUG_HTML_BEGIN"
DEBUG_MARKER_END = "AGENTATION_DEBUG_HTML_END"

DEFAULT_DEBUG_HTML_REL = Path("target/web/debug.html")
DEFAULT_OVERLAY_DIR_REL = Path("target/web/debug/agentation")
OVERLAY_JS_NAME = "agentation-overlay.js"


def _resolve_path(project_dir: Path, configured: Path | None, default_rel: Path) -> Path:
    if configured is None:
        return project_dir / default_rel
    raw = configured.expanduser()
    return raw if raw.is_absolute() else (project_dir / raw)


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
        raise SystemExit("stale agentation managed markers found in build.gradle, but block is malformed")
    print("cleaned stale managed agentation hook from build.gradle")
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


def _debug_marker_pattern() -> re.Pattern[bytes]:
    return re.compile(
        rb"(?:\r?\n)?[ \t]*<!-- "
        + re.escape(DEBUG_MARKER_BEGIN.encode("utf-8"))
        + rb" -->.*?<!-- "
        + re.escape(DEBUG_MARKER_END.encode("utf-8"))
        + rb" -->(?:\r?\n)?",
        re.S,
    )


def _overlay_script_src(debug_html: Path, overlay_dir: Path) -> str:
    overlay_js = overlay_dir / OVERLAY_JS_NAME
    try:
        rel = overlay_js.relative_to(debug_html.parent)
    except ValueError:
        rel = Path(os.path.relpath(overlay_js, debug_html.parent))
    return rel.as_posix()


def _inject_debug_html(debug_html: Path, script_src: str) -> None:
    if not debug_html.exists():
        raise SystemExit(f"debug html not found: {debug_html}")
    data = debug_html.read_bytes()
    marker = _debug_marker_pattern()
    if marker.search(data):
        print(f"debug html already injected: {debug_html}")
        return

    newline = detect_newline(data)
    snippet = (
        b"<!-- " + DEBUG_MARKER_BEGIN.encode("utf-8") + b" -->" + newline
        + b"<script type=\"text/javascript\">" + newline
        + b"    window.__DOCZILLA_AGENTATION_CONFIG__ = window.__DOCZILLA_AGENTATION_CONFIG__ || {};" + newline
        + b"</script>" + newline
        + f"<script type=\"text/javascript\" src=\"{script_src}\"></script>".encode("utf-8")
        + newline
        + b"<!-- " + DEBUG_MARKER_END.encode("utf-8") + b" -->" + newline
    )

    body_close = re.compile(rb"(?i)</body>")
    m = body_close.search(data)
    if m:
        insert = newline + snippet if m.start() > 0 and data[m.start() - 1 : m.start()] not in (b"\n", b"\r") else snippet
        patched = data[: m.start()] + insert + data[m.start() :]
    else:
        suffix = b"" if data.endswith((b"\n", b"\r")) else newline
        patched = data + suffix + snippet
    debug_html.write_bytes(patched)
    print(f"injected agentation into {debug_html}")


def _remove_debug_html_injection(debug_html: Path) -> None:
    if not debug_html.exists():
        print(f"debug html not found (skip): {debug_html}")
        return
    data = debug_html.read_bytes()
    patched = _debug_marker_pattern().sub(b"", data, count=1)
    if patched == data:
        print(f"debug html injection already absent: {debug_html}")
        return
    debug_html.write_bytes(patched)
    print(f"removed agentation injection from {debug_html}")


def inject_agentation(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    build_gradle = project_dir / "build.gradle"
    if not build_gradle.is_file():
        raise SystemExit(f"build.gradle not found: {build_gradle}")

    debug_html = _resolve_path(project_dir, ctx.debug_html_path, DEFAULT_DEBUG_HTML_REL)
    overlay_dir = _resolve_path(project_dir, ctx.overlay_dir_path, DEFAULT_OVERLAY_DIR_REL)
    overlay_script_src = _overlay_script_src(debug_html, overlay_dir)

    build_baseline = _remove_managed_build_block(build_gradle.read_bytes())
    if PROJECT_AGENTATION_HOOK_MARKER.encode("utf-8") in build_baseline:
        print("project agentation hook already enabled in build.gradle; using project hook")
        run_gradle_task(project_dir, "agentationOverlay")
        _inject_debug_html(debug_html, overlay_script_src)
        return 0

    payload = payload_text("agentation_gradle")
    if "AGENTATION_GRADLE_HOOK_BEGIN" not in payload:
        raise SystemExit("invalid agentation payload: AGENTATION_GRADLE_HOOK_BEGIN marker is missing")

    run_dir = project_dir / ".dzsc" / "run" / f"agentation-{uuid.uuid4().hex[:8]}"
    payload_target = run_dir / "gradle" / "agentation-debug-overlay.gradle"

    try:
        payload_target.parent.mkdir(parents=True, exist_ok=True)
        payload_target.write_text(payload, encoding="utf-8")

        apply_rel = payload_target.relative_to(project_dir).as_posix()
        build_gradle.write_bytes(_insert_managed_build_block(build_baseline, apply_rel))

        run_gradle_task(project_dir, "agentationOverlay")
        _inject_debug_html(debug_html, overlay_script_src)
        return 0
    finally:
        build_gradle.write_bytes(build_baseline)
        shutil.rmtree(run_dir, ignore_errors=True)
        rmdir_if_empty(project_dir / ".dzsc" / "run")
        rmdir_if_empty(project_dir / ".dzsc")


def remove_agentation(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    debug_html = _resolve_path(project_dir, ctx.debug_html_path, DEFAULT_DEBUG_HTML_REL)
    overlay_dir = _resolve_path(project_dir, ctx.overlay_dir_path, DEFAULT_OVERLAY_DIR_REL)

    _remove_debug_html_injection(debug_html)
    if overlay_dir.exists():
        shutil.rmtree(overlay_dir)
        print(f"deleted overlay output dir: {overlay_dir}")
    else:
        print(f"overlay output dir already absent: {overlay_dir}")
    return 0


def agentation_status(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    build_gradle = project_dir / "build.gradle"
    debug_html = _resolve_path(project_dir, ctx.debug_html_path, DEFAULT_DEBUG_HTML_REL)
    overlay_js = _resolve_path(project_dir, ctx.overlay_dir_path, DEFAULT_OVERLAY_DIR_REL) / OVERLAY_JS_NAME

    build_data = build_gradle.read_bytes() if build_gradle.exists() else b""
    html_data = debug_html.read_bytes() if debug_html.exists() else b""
    print(
        f"Project agentation hook in build.gradle: "
        f"{'ENABLED' if PROJECT_AGENTATION_HOOK_MARKER.encode('utf-8') in build_data else 'DISABLED'}"
    )
    print(
        f"Root build hook (should be absent after one-shot): "
        f"{'PRESENT' if BUILD_MARKER_START.encode('utf-8') in build_data else 'ABSENT'}"
    )
    print(
        f"target/web/debug.html injection: "
        f"{'ENABLED' if DEBUG_MARKER_BEGIN.encode('utf-8') in html_data else 'DISABLED'}"
    )
    print(f"overlay bundle: {'PRESENT' if overlay_js.exists() else 'MISSING'}")
    return 0


INJECT_AGENTATION_STAGE = stage(
    "inject_agentation",
    "Build/inject agentation overlay into target/web/debug.html (one-shot temporary Gradle hook)",
    aliases=("inject-agentation", "generate_agentation"),
)(inject_agentation)

REMOVE_AGENTATION_STAGE = stage(
    "remove_agentation",
    "Remove managed agentation injection from debug.html and delete overlay output dir",
    aliases=("remove-agentation",),
)(remove_agentation)

AGENTATION_STATUS_STAGE = stage(
    "agentation_status",
    "Show agentation overlay/debug.html injection status",
    aliases=("agentation-status",),
)(agentation_status)

