from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from dzsc.common import detect_newline, ensure_project_dir, make_user_executable, restore_file, rmdir_if_empty
from dzsc.common import resolve_gradle_wrapper
from dzsc.payloads import payload_bytes, payload_text
from dzsc.sdk import StageRunContext, stage


MANAGED_START_MARKER = "// >>> z8-debug-sourcemaps (managed)"
MANAGED_END_MARKER = "// <<< z8-debug-sourcemaps (managed)"
MANAGED_APPLY_LINES = (
    "apply from: '.dzsc/gradle/z8-debug-sourcemaps.gradle'",
    "apply from: 'gradle/z8-debug-sourcemaps.gradle'",  # legacy path from older helper versions
)


def _strip_stale_managed_build_block(build_gradle: Path) -> None:
    data = build_gradle.read_bytes()
    start_b = MANAGED_START_MARKER.encode("utf-8")
    end_b = MANAGED_END_MARKER.encode("utf-8")
    has_start = start_b in data
    has_end = end_b in data
    if not has_start and not has_end:
        return

    nl = detect_newline(data)
    for apply_line in MANAGED_APPLY_LINES:
        apply_b = apply_line.encode("utf-8")
        exact_block = nl + start_b + nl + apply_b + nl + end_b + nl
        if exact_block in data:
            patched = data.replace(exact_block, b"", 1)
            build_gradle.write_bytes(patched)
            print(f"cleaned stale managed sourcemap hook from {build_gradle}")
            return

    for apply_line in MANAGED_APPLY_LINES:
        apply_b = apply_line.encode("utf-8")
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
            print(f"cleaned stale managed sourcemap hook from {build_gradle} (regex fallback)")
            return

    raise SystemExit(
        f"managed sourcemap markers exist in {build_gradle}, but block shape is unexpected; fix manually"
    )


def _same_file_content(path: Path, payload: bytes) -> bool:
    return path.is_file() and path.read_bytes() == payload


def _remove_stale_payload_file(path: Path, payload: bytes) -> bool:
    if not _same_file_content(path, payload):
        return False
    path.unlink()
    print(f"removed stale managed payload: {path}")
    return True


def _parse_csv_list(raw: str | None) -> list[str]:
    if raw is None:
        return []
    values: list[str] = []
    for chunk in raw.replace(";", ",").split(","):
        item = chunk.strip().strip("/").replace("\\", "/")
        if item:
            values.append(item)
    return values


def _parse_simple_yaml_lists_text(raw_text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}

    current_key: str | None = None
    for raw_line in raw_text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            stripped = line.strip()
            if current_key and stripped.startswith("- "):
                result.setdefault(current_key, []).append(stripped[2:].strip().strip("'\""))
            continue

        current_key = None
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value:
                result[key] = [value.strip().strip("'\"")]
            else:
                result.setdefault(key, [])
                current_key = key
    return result


def _parse_simple_yaml_lists(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    return _parse_simple_yaml_lists_text(path.read_text(encoding="utf-8"))


def _normalize_rel_list(values: tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = value.strip().strip("/").replace("\\", "/")
        if item:
            normalized.append(item)
    return normalized


def _resolve_list_setting(
    env_name: str,
    yaml_key: str,
    config: dict[str, list[str]],
    default: list[str],
    cli_override: tuple[str, ...],
) -> list[str]:
    if cli_override:
        return _normalize_rel_list(cli_override)
    override = _parse_csv_list(os.environ.get(env_name))
    if override:
        return override
    values = [v.strip().strip("/").replace("\\", "/") for v in config.get(yaml_key, []) if v.strip()]
    return values or default


def run_dz_source_maps(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    python_bin = ctx.python_bin or sys.executable
    sm_payload_bytes = payload_bytes("sourcemap_gradle")
    toggle_payload_bytes = payload_bytes("sourcemap_toggle_py")

    build_gradle = project_dir / "build.gradle"
    dzsc_dir = project_dir / ".dzsc"
    tmp_dir = dzsc_dir / "tmp"
    sm_gradle = dzsc_dir / "gradle" / "z8-debug-sourcemaps.gradle"
    toggle_script = dzsc_dir / "tools" / "toggle_z8_debug_sourcemaps.py"
    legacy_sm_gradle = project_dir / "gradle" / "z8-debug-sourcemaps.gradle"
    legacy_toggle_script = project_dir / "tools" / "toggle_z8_debug_sourcemaps.py"

    if not build_gradle.is_file():
        raise SystemExit(f"build.gradle not found: {build_gradle}")
    # Validates wrapper presence/executability (and normalizes Windows .bat path).
    gradle_cmd_base = resolve_gradle_wrapper(project_dir)

    _strip_stale_managed_build_block(build_gradle)

    removed_legacy_sm = _remove_stale_payload_file(legacy_sm_gradle, sm_payload_bytes)
    removed_legacy_toggle = _remove_stale_payload_file(legacy_toggle_script, toggle_payload_bytes)
    if removed_legacy_sm:
        rmdir_if_empty(legacy_sm_gradle.parent)
    if removed_legacy_toggle:
        rmdir_if_empty(legacy_toggle_script.parent)

    if ctx.sourcemap_config:
        config = _parse_simple_yaml_lists(ctx.sourcemap_config)
    else:
        config = _parse_simple_yaml_lists_text(payload_text("sourcemap_config"))
    concat_source_roots = _resolve_list_setting(
        "Z8_SM_CONCAT_SOURCE_ROOTS",
        "concat_source_roots",
        config,
        ["src/main/js", "src/js"],
        ctx.concat_source_roots,
    )
    local_project_search_roots = _resolve_list_setting(
        "Z8_SM_LOCAL_PROJECT_SEARCH_ROOTS",
        "local_project_search_roots",
        config,
        [".", "org.zenframework.z8"],
        ctx.local_project_search_roots,
    )

    had_dzsc_dir = dzsc_dir.is_dir()
    had_dzsc_gradle_dir = (dzsc_dir / "gradle").is_dir()
    had_dzsc_tools_dir = (dzsc_dir / "tools").is_dir()

    stale_sm_gradle = _same_file_content(sm_gradle, sm_payload_bytes)
    stale_toggle_script = _same_file_content(toggle_script, toggle_payload_bytes)
    if stale_sm_gradle:
        print(f"detected stale managed payload (will remove after run): {sm_gradle}")
    if stale_toggle_script:
        print(f"detected stale managed payload (will remove after run): {toggle_script}")

    had_sm_gradle = sm_gradle.exists() and not stale_sm_gradle
    had_toggle_script = toggle_script.exists() and not stale_toggle_script

    build_backup = tmp_dir / "build.gradle.bak"
    sm_gradle_backup = tmp_dir / "z8-debug-sourcemaps.gradle.orig"
    toggle_backup = tmp_dir / "toggle_z8_debug_sourcemaps.py.orig"

    tmp_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build_gradle, build_backup)
    if had_sm_gradle:
        shutil.copy2(sm_gradle, sm_gradle_backup)
    if had_toggle_script:
        shutil.copy2(toggle_script, toggle_backup)

    try:
        sm_gradle.parent.mkdir(parents=True, exist_ok=True)
        toggle_script.parent.mkdir(parents=True, exist_ok=True)
        sm_gradle.write_bytes(sm_payload_bytes)
        toggle_script.write_bytes(toggle_payload_bytes)
        make_user_executable(toggle_script)

        subprocess.run([python_bin, str(toggle_script), "on", str(build_gradle)], cwd=project_dir, check=True)

        gradle_env = os.environ.copy()
        gradle_env["Z8_SM_CONCAT_SOURCE_ROOTS"] = ",".join(concat_source_roots)
        gradle_env["Z8_SM_LOCAL_PROJECT_SEARCH_ROOTS"] = ",".join(local_project_search_roots)
        subprocess.run([*gradle_cmd_base, "generateDebugJsSourceMap"], cwd=project_dir, env=gradle_env, check=True)
        return 0
    finally:
        restore_file(build_gradle, build_backup)
        restore_file(sm_gradle, sm_gradle_backup if had_sm_gradle else None)
        restore_file(toggle_script, toggle_backup if had_toggle_script else None)
        if not had_dzsc_tools_dir:
            rmdir_if_empty(dzsc_dir / "tools")
        if not had_dzsc_gradle_dir:
            rmdir_if_empty(dzsc_dir / "gradle")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if not had_dzsc_dir:
            rmdir_if_empty(dzsc_dir)


DZ_SOURCE_MAPS_STAGE = stage(
    "dz_source_maps",
    "One-shot generateDebugJsSourceMap with temporary Gradle hook and cleanup",
    aliases=("dz-source-maps", "generate_debug_js_sourcemap"),
)(run_dz_source_maps)
