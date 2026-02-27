from __future__ import annotations

import sys
from pathlib import Path

import rich_click as click

from dzsc.builtin import build_registry
from dzsc.common import default_python_bin
from dzsc.sdk import Stage, StageRunContext


_COMMAND_NAMES = {"run", "stages"}


def _registry() -> "StageRegistry":
    # Late import type only to avoid circular typing import at runtime.
    return build_registry()


def _stage_completion(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[str]:
    del ctx, param
    items: list[str] = []
    for st in _registry().list():
        if st.stage_id.startswith(incomplete):
            items.append(st.stage_id)
    return items


def _print_stage_list() -> None:
    for st in _registry().list():
        aliases = ", ".join(st.aliases)
        alias_suffix = f" (aliases: {aliases})" if aliases else ""
        click.echo(f"{st.stage_id:20} {st.description}{alias_suffix}")


def _run_pipeline(ctx_obj: StageRunContext, stages: tuple[str, ...]) -> int:
    registry = _registry()
    resolved = registry.resolve_pipeline(stages)
    total = len(resolved)

    for idx, stage in enumerate(resolved, start=1):
        click.echo(f"[{idx}/{total}] {stage.stage_id}")
        result = stage.handler(ctx_obj)
        if isinstance(result, int) and result != 0:
            return result

    return 0


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Doczilla one-shot stage controller."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("run")
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
@click.option(
    "--sourcemap-config",
    type=click.Path(path_type=Path, dir_okay=False, file_okay=True),
    default=None,
    help="Optional frontend sourcemap config YAML override.",
)
@click.option(
    "--concat-source-root",
    "concat_source_roots",
    multiple=True,
    help="Override concat source roots for dz_source_maps (repeatable).",
)
@click.option(
    "--local-project-search-root",
    "local_project_search_roots",
    multiple=True,
    help="Override local project search roots for dz_source_maps (repeatable).",
)
@click.option("--verbose", is_flag=True, help="Verbose output (reserved for future diagnostics).")
@click.argument("stages", nargs=-1, required=True, shell_complete=_stage_completion)
def run_command(
    project_dir: Path,
    python_bin: str,
    sourcemap_config: Path | None,
    concat_source_roots: tuple[str, ...],
    local_project_search_roots: tuple[str, ...],
    verbose: bool,
    stages: tuple[str, ...],
) -> None:
    """Run stages in the order provided."""
    ctx_obj = StageRunContext(
        project_dir=project_dir.expanduser().resolve(),
        python_bin=python_bin,
        sourcemap_config=sourcemap_config.expanduser().resolve() if sourcemap_config else None,
        concat_source_roots=tuple(concat_source_roots),
        local_project_search_roots=tuple(local_project_search_roots),
        verbose=verbose,
    )
    rc = _run_pipeline(ctx_obj, stages)
    if rc:
        raise SystemExit(rc)


@cli.group("stages")
def stages_group() -> None:
    """Inspect built-in stages."""


@stages_group.command("list")
def stages_list_command() -> None:
    """List built-in stages."""
    _print_stage_list()


def _rewrite_compat_argv(argv: list[str]) -> list[str]:
    if "--stages" not in argv:
        return argv

    out: list[str] = []
    removed = False
    for item in argv:
        if not removed and item == "--stages":
            removed = True
            continue
        out.append(item)

    first_non_option = next((a for a in out if not a.startswith("-")), None)
    if first_non_option not in _COMMAND_NAMES:
        out = ["run", *out]

    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    args = _rewrite_compat_argv(args)
    cli.main(args=args, prog_name="dzsc", standalone_mode=False)
    return 0

