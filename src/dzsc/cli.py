from __future__ import annotations

from dataclasses import dataclass, replace
import sys
from pathlib import Path

import rich_click as click

from dzsc.builtin import build_registry
from dzsc.common import default_python_bin
from dzsc.sdk import StageInvocation, StageRunContext


STAGE_MARKERS = {"-stage", "--stage"}


@dataclass(frozen=True, slots=True)
class _StageOptionDef:
    ctx_field: str
    value_kind: str
    multiple: bool = False


STAGE_OPTION_MAP: dict[str, dict[str, _StageOptionDef]] = {
    "dz_source_maps": {
        "--sourcemap-config": _StageOptionDef("sourcemap_config", "path"),
        "--concat-source-root": _StageOptionDef("concat_source_roots", "str", multiple=True),
        "--local-project-search-root": _StageOptionDef("local_project_search_roots", "str", multiple=True),
    },
    "inject_agentation": {
        "--debug-path": _StageOptionDef("debug_html_path", "path"),
    },
    "remove_agentation": {
        "--debug-path": _StageOptionDef("debug_html_path", "path"),
        "--overlay-dir": _StageOptionDef("overlay_dir_path", "path"),
    },
    "agentation_status": {
        "--debug-path": _StageOptionDef("debug_html_path", "path"),
        "--overlay-dir": _StageOptionDef("overlay_dir_path", "path"),
    },
}


def _registry():
    return build_registry()


def _normalize_option_name(raw_name: str) -> str:
    if not raw_name.startswith("-"):
        return raw_name
    name = raw_name.lstrip("-").strip().lower().replace("_", "-")
    return f"--{name}"


def _convert_option_value(value_kind: str, raw_value: str) -> object:
    if value_kind == "path":
        return Path(raw_value).expanduser()
    return raw_value


def _completion(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[str]:
    del ctx, param
    if incomplete.startswith("-"):
        return [m for m in sorted(STAGE_MARKERS) if m.startswith(incomplete)]
    return [stage.stage_id for stage in _registry().list() if stage.stage_id.startswith(incomplete)]


def _parse_pipeline_tokens(tokens: tuple[str, ...]) -> list[StageInvocation]:
    if not tokens:
        raise click.UsageError("No stages specified. Use: dzsc run -stage <id> [-opt value] -stage <id> ...")
    if not any(token in STAGE_MARKERS for token in tokens):
        raise click.UsageError("Pipeline must use '-stage <id>' blocks.")

    registry = _registry()
    invocations: list[StageInvocation] = []
    current: StageInvocation | None = None
    idx = 0

    while idx < len(tokens):
        token = tokens[idx]
        if token in STAGE_MARKERS:
            idx += 1
            if idx >= len(tokens):
                raise click.UsageError("Expected stage id after '-stage'.")
            stage_name = tokens[idx]
            if stage_name.startswith("-"):
                raise click.UsageError(f"Expected stage id after '-stage', got '{stage_name}'.")
            try:
                registry.resolve(stage_name)
            except KeyError as exc:
                raise click.UsageError(str(exc)) from exc
            current = StageInvocation(stage_name=stage_name)
            invocations.append(current)
            idx += 1
            continue

        if current is None:
            raise click.UsageError("Token before first stage block. Start with '-stage <id>'.")
        if not token.startswith("-"):
            raise click.UsageError(f"Unexpected token '{token}' in stage '{current.stage_name}'.")

        option_token = token
        raw_value: str | None = None
        if "=" in option_token:
            option_token, raw_value = option_token.split("=", 1)

        stage = registry.resolve(current.stage_name)
        option_key = _normalize_option_name(option_token)
        option_def = STAGE_OPTION_MAP.get(stage.stage_id, {}).get(option_key)
        if option_def is None:
            allowed = ", ".join(sorted(STAGE_OPTION_MAP.get(stage.stage_id, {}).keys())) or "<none>"
            raise click.UsageError(
                f"Unknown option '{option_token}' for stage '{current.stage_name}'. Allowed: {allowed}"
            )

        if raw_value is None:
            if idx + 1 >= len(tokens) or tokens[idx + 1] in STAGE_MARKERS:
                raise click.UsageError(f"Option '{option_token}' requires a value.")
            raw_value = tokens[idx + 1]
            idx += 1

        value = _convert_option_value(option_def.value_kind, raw_value)
        if option_def.multiple:
            bucket = current.overrides.setdefault(option_def.ctx_field, [])
            if not isinstance(bucket, list):
                bucket = [bucket]
            bucket.append(value)
            current.overrides[option_def.ctx_field] = bucket
        else:
            current.overrides[option_def.ctx_field] = value
        idx += 1

    for invocation in invocations:
        for key, value in list(invocation.overrides.items()):
            if isinstance(value, list):
                invocation.overrides[key] = tuple(value)
    return invocations


def _run_pipeline(base_ctx: StageRunContext, invocations: list[StageInvocation]) -> int:
    registry = _registry()
    prepared: list[tuple[object, dict[str, object]]] = []
    emitted_deps: set[str] = set()

    for invocation in invocations:
        target = registry.resolve(invocation.stage_name)
        for stage in registry.resolve_pipeline((invocation.stage_name,)):
            is_target = stage.stage_id == target.stage_id
            if not is_target and stage.stage_id in emitted_deps:
                continue
            prepared.append((stage, invocation.overrides if is_target else {}))
            emitted_deps.add(stage.stage_id)

    total = len(prepared)
    for idx, (stage, overrides) in enumerate(prepared, start=1):
        click.echo(f"[{idx}/{total}] {stage.stage_id}")
        stage_ctx = replace(base_ctx, **overrides) if overrides else base_ctx
        result = stage.handler(stage_ctx)
        if isinstance(result, int) and result != 0:
            return result
    return 0


def _print_stage_list() -> None:
    for stage in _registry().list():
        aliases = ", ".join(stage.aliases)
        suffix = f" (aliases: {aliases})" if aliases else ""
        click.echo(f"{stage.stage_id:20} {stage.description}{suffix}")


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Doczilla one-shot stage controller."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("run", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option(
    "--project",
    "project_dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("."),
    show_default=True,
    help="Target Doczilla project root (defaults to current directory).",
)
@click.option(
    "--python",
    "python_bin",
    default=default_python_bin,
    show_default=True,
    help="Python interpreter used for helper scripts.",
)
@click.option("--verbose", is_flag=True, help="Verbose output.")
@click.argument("pipeline_tokens", nargs=-1, required=True, type=click.UNPROCESSED, shell_complete=_completion)
def run_command(project_dir: Path, python_bin: str, verbose: bool, pipeline_tokens: tuple[str, ...]) -> None:
    """Run pipeline with strict stage blocks.

    Example:
      dzsc run -stage dz_source_maps --sourcemap-config ./cfg.yml -stage inject_agentation --debug-path ./target/web/debug.html
    """
    invocations = _parse_pipeline_tokens(pipeline_tokens)
    base_ctx = StageRunContext(
        project_dir=project_dir.expanduser().resolve(),
        python_bin=python_bin,
        verbose=verbose,
    )
    rc = _run_pipeline(base_ctx, invocations)
    if rc:
        raise SystemExit(rc)


@cli.group("stages")
def stages_group() -> None:
    """Inspect built-in stages."""


@stages_group.command("list")
def stages_list_command() -> None:
    _print_stage_list()


def _rewrite_root_shortcut(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    if argv[0] in {"run", "stages"}:
        return argv
    if any(token in STAGE_MARKERS for token in argv):
        return ["run", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    args = _rewrite_root_shortcut(args)
    try:
        cli.main(args=args, prog_name="dzsc", standalone_mode=False)
        return 0
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
