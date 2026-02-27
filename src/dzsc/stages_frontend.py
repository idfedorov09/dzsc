from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from dzsc.common import detect_newline, ensure_project_dir, resolve_gradle_wrapper, rmdir_if_empty
from dzsc.payloads import payload_bytes, payload_text
from dzsc.sdk import StageRunContext, stage


MANAGED_START_MARKER = "// >>> z8-debug-sourcemaps (managed)"
MANAGED_END_MARKER = "// <<< z8-debug-sourcemaps (managed)"
DEFAULT_CONCAT_SOURCE_ROOTS = ["src/main/js", "src/js"]
DEFAULT_LOCAL_PROJECT_SEARCH_ROOTS = [".", "org.zenframework.z8"]


def _managed_block(newline: bytes, apply_rel: str) -> bytes:
    return (
        newline
        + MANAGED_START_MARKER.encode("utf-8")
        + newline
        + f"apply from: '{apply_rel}'".encode("utf-8")
        + newline
        + MANAGED_END_MARKER.encode("utf-8")
        + newline
    )


def _remove_managed_block(build_data: bytes) -> bytes:
    start = MANAGED_START_MARKER.encode("utf-8")
    end = MANAGED_END_MARKER.encode("utf-8")
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
        raise SystemExit("stale sourcemap managed markers found in build.gradle, but block is malformed")
    print("cleaned stale managed sourcemap hook from build.gradle")
    return patched


def _insert_managed_block(build_data: bytes, apply_rel: str) -> bytes:
    newline = detect_newline(build_data)
    block = _managed_block(newline, apply_rel)
    anchor = re.compile(rb"(?m)^apply from: ['\"]docker\.gradle['\"]\r?$$")
    matches = list(anchor.finditer(build_data))
    if matches:
        idx = matches[-1].start()
        return build_data[:idx] + block + build_data[idx:]
    suffix = b"" if build_data.endswith((b"\n", b"\r")) else newline
    return build_data + suffix + block


def _normalize_rel_path(raw: str) -> str:
    return raw.strip().strip("/").replace("\\", "/")


def _parse_simple_yaml_lists(raw_text: str) -> dict[str, list[str]]:
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


def _setting_from_config(
    config: dict[str, list[str]],
    key: str,
    default: list[str],
    override: tuple[str, ...],
) -> list[str]:
    if override:
        return [_normalize_rel_path(v) for v in override if _normalize_rel_path(v)]
    values = [_normalize_rel_path(v) for v in config.get(key, []) if _normalize_rel_path(v)]
    return values or list(default)


def run_dz_source_maps(ctx: StageRunContext) -> int:
    project_dir = ctx.project_dir
    ensure_project_dir(project_dir)

    build_gradle = project_dir / "build.gradle"
    if not build_gradle.is_file():
        raise SystemExit(f"build.gradle not found: {build_gradle}")

    gradle_cmd_base = resolve_gradle_wrapper(project_dir)
    gradle_payload = payload_bytes("sourcemap_gradle")
    config_text = (
        ctx.sourcemap_config.read_text(encoding="utf-8")
        if ctx.sourcemap_config is not None
        else payload_text("sourcemap_config")
    )
    config = _parse_simple_yaml_lists(config_text)
    concat_roots = _setting_from_config(
        config,
        "concat_source_roots",
        DEFAULT_CONCAT_SOURCE_ROOTS,
        ctx.concat_source_roots,
    )
    local_roots = _setting_from_config(
        config,
        "local_project_search_roots",
        DEFAULT_LOCAL_PROJECT_SEARCH_ROOTS,
        ctx.local_project_search_roots,
    )

    baseline_build = _remove_managed_block(build_gradle.read_bytes())
    run_dir = project_dir / ".dzsc" / "run" / f"dz-source-maps-{uuid.uuid4().hex[:8]}"
    payload_target = run_dir / "gradle" / "z8-debug-sourcemaps.gradle"

    try:
        payload_target.parent.mkdir(parents=True, exist_ok=True)
        payload_target.write_bytes(gradle_payload)

        apply_rel = payload_target.relative_to(project_dir).as_posix()
        build_gradle.write_bytes(_insert_managed_block(baseline_build, apply_rel))

        gradle_env = os.environ.copy()
        gradle_env["Z8_SM_CONCAT_SOURCE_ROOTS"] = ",".join(concat_roots)
        gradle_env["Z8_SM_LOCAL_PROJECT_SEARCH_ROOTS"] = ",".join(local_roots)
        subprocess.run([*gradle_cmd_base, "generateDebugJsSourceMap"], cwd=project_dir, env=gradle_env, check=True)
        return 0
    finally:
        build_gradle.write_bytes(baseline_build)
        shutil.rmtree(run_dir, ignore_errors=True)
        rmdir_if_empty(project_dir / ".dzsc" / "run")
        rmdir_if_empty(project_dir / ".dzsc")


DZ_SOURCE_MAPS_STAGE = stage(
    "dz_source_maps",
    "One-shot generateDebugJsSourceMap with temporary Gradle hook and cleanup",
    aliases=("dz-source-maps", "generate_debug_js_sourcemap"),
)(run_dz_source_maps)
