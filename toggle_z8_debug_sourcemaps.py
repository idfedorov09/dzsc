#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

START_MARKER = "// >>> z8-debug-sourcemaps (managed)"
END_MARKER = "// <<< z8-debug-sourcemaps (managed)"
APPLY_LINE = "apply from: '.dzsc/gradle/z8-debug-sourcemaps.gradle'"
DOCKER_APPLY_LINE = "apply from: 'docker.gradle'"


def detect_newline(data: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in data else b"\n"


def managed_block(newline: bytes) -> bytes:
    return (
        newline
        + START_MARKER.encode("utf-8")
        + newline
        + APPLY_LINE.encode("utf-8")
        + newline
        + END_MARKER.encode("utf-8")
        + newline
    )


def patch_on(build_gradle: Path) -> int:
    data = build_gradle.read_bytes()
    nl = detect_newline(data)
    block = managed_block(nl)

    if block in data:
        print(f"already enabled: {build_gradle}")
        return 0

    if START_MARKER.encode("utf-8") in data or END_MARKER.encode("utf-8") in data:
        raise SystemExit(f"managed markers already exist in {build_gradle}, but block is not exact; fix manually")

    anchor = DOCKER_APPLY_LINE.encode("utf-8")
    idx = data.rfind(anchor)
    if idx >= 0:
        patched = data[:idx] + block + data[idx:]
    else:
        suffix = b"" if data.endswith((b"\n", b"\r")) else nl
        patched = data + suffix + block

    if patched != data:
        build_gradle.write_bytes(patched)
        print(f"enabled: inserted managed sourcemap apply block into {build_gradle}")
    else:
        print(f"no changes: {build_gradle}")
    return 0


def cleanup_generated_artifacts(project_dir: Path) -> None:
    debug_html = project_dir / "target" / "web" / "debug.html"
    debug_js = project_dir / "target" / "web" / "debug" / f"{project_dir.name}.js"

    if debug_html.exists():
        m = re.search(r'src="debug/([^"?#]+\.js)"', debug_html.read_text("utf-8", errors="ignore"))
        if m:
            debug_js = project_dir / "target" / "web" / "debug" / m.group(1)

    if debug_js.exists():
        js_bytes = debug_js.read_bytes()
        cleaned = re.sub(rb'\r?\n?//# sourceMappingURL=[^\r\n]+\s*\Z', b'', js_bytes, flags=re.S)
        if cleaned != js_bytes:
            # preserve trailing newline if file had one before the mapping comment was appended
            if js_bytes.endswith(b"\r\n") and not cleaned.endswith(b"\r\n"):
                cleaned += b"\r\n"
            elif js_bytes.endswith(b"\n") and not cleaned.endswith((b"\n", b"\r\n")):
                cleaned += b"\n"
            debug_js.write_bytes(cleaned)
            print(f"removed sourceMappingURL: {debug_js}")

        map_file = debug_js.with_name(debug_js.name + ".map")
        if map_file.exists():
            map_file.unlink()
            print(f"deleted: {map_file}")


def patch_off(build_gradle: Path) -> int:
    data = build_gradle.read_bytes()
    nl = detect_newline(data)
    block = managed_block(nl)

    if block in data:
        patched = data.replace(block, b"", 1)
        build_gradle.write_bytes(patched)
        print(f"disabled: removed managed sourcemap apply block from {build_gradle}")
    else:
        text = data.decode("utf-8")
        pattern = re.compile(
            r"(?:\r?\n)?" + re.escape(START_MARKER) + r"\r?\n" + re.escape(APPLY_LINE) + r"\r?\n" + re.escape(END_MARKER) + r"(?:\r?\n)?",
            re.M,
        )
        patched_text, count = pattern.subn("", text, count=1)
        if count:
            build_gradle.write_text(patched_text, encoding="utf-8", newline="" if nl == b"\n" else "\r\n")
            print(f"disabled: removed managed sourcemap apply block from {build_gradle} (regex fallback)")
        else:
            print(f"already disabled: {build_gradle}")

    cleanup_generated_artifacts(build_gradle.parent)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"on", "off"}:
        print("usage: toggle_z8_debug_sourcemaps.py <on|off> [build.gradle]", file=sys.stderr)
        return 2

    build_gradle = Path(argv[2]) if len(argv) > 2 else Path("build.gradle")
    if not build_gradle.exists():
        print(f"build.gradle not found: {build_gradle}", file=sys.stderr)
        return 2

    if argv[1] == "on":
        return patch_on(build_gradle)
    return patch_off(build_gradle)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
