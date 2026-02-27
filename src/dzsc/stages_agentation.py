from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from dzsc.common import detect_newline, ensure_project_dir, restore_file, rmdir_if_empty, run_gradle_task
from dzsc.payloads import payload_bytes, payload_text
from dzsc.sdk import StageRunContext, stage


BUILD_MARKER_START = "// >>> dzsc-agentation-debug (managed)"
BUILD_MARKER_END = "// <<< dzsc-agentation-debug (managed)"
BUILD_APPLY_REL = ".dzsc/gradle/agentation-debug-overlay.gradle"
LEGACY_BUILD_APPLY_REL = "gradle/.codex-agentation-debug.gradle"
BUILD_APPLY_RELS = (BUILD_APPLY_REL, LEGACY_BUILD_APPLY_REL)
PROJECT_AGENTATION_HOOK_MARKER = "AGENTATION_GRADLE_HOOK_BEGIN"

DEBUG_MARKER_BEGIN = "AGENTATION_DEBUG_HTML_BEGIN"
DEBUG_MARKER_END = "AGENTATION_DEBUG_HTML_END"
DEBUG_HTML_REL = Path("target/web/debug.html")
OVERLAY_DIR_REL = Path("target/web/debug/agentation")
OVERLAY_JS_REL = OVERLAY_DIR_REL / "agentation-overlay.js"


def _managed_build_block(newline: bytes, apply_rel: str = BUILD_APPLY_REL) -> bytes:
    return (
        newline
        + BUILD_MARKER_START.encode("utf-8")
        + newline
        + f"apply from: '{apply_rel}'".encode("utf-8")
        + newline
        + BUILD_MARKER_END.encode("utf-8")
        + newline
    )


def _strip_stale_managed_build_block(build_gradle: Path) -> None:
    data = build_gradle.read_bytes()
    start_b = BUILD_MARKER_START.encode("utf-8")
    end_b = BUILD_MARKER_END.encode("utf-8")
    if start_b not in data and end_b not in data:
        return

    nl = detect_newline(data)
    for apply_rel in BUILD_APPLY_RELS:
        block = _managed_build_block(nl, apply_rel=apply_rel)
        if block in data:
            build_gradle.write_bytes(data.replace(block, b"", 1))
            print(f"cleaned stale managed agentation hook from {build_gradle}")
            return

    for apply_rel in BUILD_APPLY_RELS:
        apply_b = f"apply from: '{apply_rel}'".encode("utf-8")
        pattern = re.compile(
            rb"(?:\r?\n)?"
            + re.escape(start_b)
            + rb"\r?\n"
            + re.escape(apply_b)
            + rb"\r?\n"
            + re.escape(end_b)
            + rb"(?:\r?\n)?",
            re.M,
        )
        patched, count = pattern.subn(b"", data, count=1)
        if count:
            build_gradle.write_bytes(patched)
            print(f"cleaned stale managed agentation hook from {build_gradle} (regex fallback)")
            return

    raise SystemExit(
        f"managed agentation markers exist in {build_gradle}, but block shape is unexpected; fix manually"
    )


def _patch_build_gradle(build_gradle: Path) -> None:
    data = build_gradle.read_bytes()
    newline = detect_newline(data)
    block = _managed_build_block(newline)

    if BUILD_MARKER_START.encode("utf-8") in data or BUILD_MARKER_END.encode("utf-8") in data:
        if block in data:
            return
        raise SystemExit(f"managed agentation markers already exist in {build_gradle}; fix manually")

    anchor_pattern = re.compile(rb"(?m)^apply from: ['\"]docker\.gradle['\"]\r?$$")
    matches = list(anchor_pattern.finditer(data))
    if matches:
        idx = matches[-1].start()
        patched = data[:idx] + block + data[idx:]
    else:
        suffix = b"" if data.endswith((b"\n", b"\r")) else newline
        patched = data + suffix + block

    build_gradle.write_bytes(patched)


def _same_file_content(path: Path, payload: bytes) -> bool:
    return path.is_file() and path.read_bytes() == payload


def _remove_stale_payload_file(path: Path, payload: bytes) -> bool:
    if not _same_file_content(path, payload):
        return False
    path.unlink()
    print(f"removed stale managed payload: {path}")
    return True


def _marker_pattern(begin: str, end: str) -> re.Pattern[bytes]:
    return re.compile(
        rb"(?:\r?\n)?[ \t]*<!-- "
        + re.escape(begin.encode("utf-8"))
        + rb" -->.*?<!-- "
        + re.escape(end.encode("utf-8"))
        + rb" -->(?:\r?\n)?",
        re.S,
    )


def _patch_debug_html(debug_html: Path, enable: bool) -> None:
    if not debug_html.exists():
        if enable:
            raise SystemExit(
                f"debug html not found: {debug_html}. Run JS/web build first (e.g. sourcemap target / assembleJs)."
            )
        print(f"debug html not found (skip): {debug_html}")
        return

    data = debug_html.read_bytes()
    newline = detect_newline(data)
    marker_re = _marker_pattern(DEBUG_MARKER_BEGIN, DEBUG_MARKER_END)

    if enable:
        if marker_re.search(data):
            print(f"debug html already injected: {debug_html}")
            return

        snippet = (
            b"<!-- " + DEBUG_MARKER_BEGIN.encode("utf-8") + b" -->" + newline
            + b"<script type=\"text/javascript\">" + newline
            + b"    window.__DOCZILLA_AGENTATION_CONFIG__ = window.__DOCZILLA_AGENTATION_CONFIG__ || {};" + newline
            + b"</script>" + newline
            + b"<script type=\"text/javascript\" src=\"debug/agentation/agentation-overlay.js\"></script>" + newline
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
        return

    patched = marker_re.sub(b"", data, count=1)
    if patched != data:
        debug_html.write_bytes(patched)
        print(f"removed agentation injection from {debug_html}")
    else:
        print(f"debug html injection already absent: {debug_html}")


def _overlay_output_status(project_dir: Path) -> None:
    build_gradle = project_dir / "build.gradle"
    debug_html = project_dir / DEBUG_HTML_REL
    overlay_js = project_dir / OVERLAY_JS_REL
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


def _load_agentation_gradle_payload() -> tuple[str, bytes]:
    text = payload_text("agentation_gradle")
    if "AGENTATION_GRADLE_HOOK_BEGIN" not in text:
        raise SystemExit("unexpected agentation payload (marker missing)")
    return text, text.encode("utf-8")


def _run_agentation_overlay(project_dir: Path) -> None:
    run_gradle_task(project_dir, "agentationOverlay")


def inject_agentation(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    build_gradle = project_dir / "build.gradle"
    if not build_gradle.exists():
        raise SystemExit(f"build.gradle not found: {build_gradle}")

    payload_text_value, payload_bytes_value = _load_agentation_gradle_payload()
    build_gradle_bytes = build_gradle.read_bytes()

    if PROJECT_AGENTATION_HOOK_MARKER.encode("utf-8") in build_gradle_bytes:
        print("project agentation hook already enabled in build.gradle; skipping temporary Gradle injection")
        _run_agentation_overlay(project_dir)
        _patch_debug_html(project_dir / DEBUG_HTML_REL, enable=True)
        return 0

    dzsc_dir = project_dir / ".dzsc"
    tmp_dir = dzsc_dir / "tmp"
    temp_apply = project_dir / BUILD_APPLY_REL
    legacy_temp_apply = project_dir / LEGACY_BUILD_APPLY_REL
    legacy_tmp_dir = project_dir / ".codex-agentation-debug-tmp"

    _strip_stale_managed_build_block(build_gradle)
    removed_legacy_temp_apply = _remove_stale_payload_file(legacy_temp_apply, payload_bytes_value)
    removed_temp_apply = _remove_stale_payload_file(temp_apply, payload_bytes_value)
    if removed_legacy_temp_apply:
        rmdir_if_empty(legacy_temp_apply.parent)
    if removed_temp_apply:
        rmdir_if_empty(temp_apply.parent)
        rmdir_if_empty(dzsc_dir)

    had_dzsc_dir = dzsc_dir.is_dir()
    had_dzsc_gradle_dir = (dzsc_dir / "gradle").is_dir()
    had_temp_apply = temp_apply.exists()

    build_backup = tmp_dir / "build.gradle.bak"
    temp_apply_backup = tmp_dir / "agentation-debug-overlay.gradle.bak"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build_gradle, build_backup)
    if had_temp_apply:
        shutil.copy2(temp_apply, temp_apply_backup)

    try:
        temp_apply.parent.mkdir(parents=True, exist_ok=True)
        temp_apply.write_text(payload_text_value, encoding="utf-8")
        _patch_build_gradle(build_gradle)
        _run_agentation_overlay(project_dir)
        _patch_debug_html(project_dir / DEBUG_HTML_REL, enable=True)
        return 0
    finally:
        restore_file(build_gradle, build_backup)
        restore_file(temp_apply, temp_apply_backup if had_temp_apply else None)
        if not had_dzsc_gradle_dir:
            rmdir_if_empty(temp_apply.parent)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if not had_dzsc_dir:
            rmdir_if_empty(dzsc_dir)
        shutil.rmtree(legacy_tmp_dir, ignore_errors=True)


def remove_agentation(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)
    _patch_debug_html(project_dir / DEBUG_HTML_REL, enable=False)
    overlay_dir = project_dir / OVERLAY_DIR_REL
    if overlay_dir.exists():
        shutil.rmtree(overlay_dir)
        print(f"deleted overlay output dir: {overlay_dir}")
    else:
        print(f"overlay output dir already absent: {overlay_dir}")
    return 0


def agentation_status(ctx: StageRunContext) -> int:
    ensure_project_dir(ctx.project_dir)
    _overlay_output_status(ctx.project_dir)
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

