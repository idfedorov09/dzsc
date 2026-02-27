from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


StageHandler = Callable[["StageRunContext"], int | None]


@dataclass(slots=True)
class StageRunContext:
    project_dir: Path
    python_bin: str
    sourcemap_config: Path | None = None
    concat_source_roots: tuple[str, ...] = ()
    local_project_search_roots: tuple[str, ...] = ()
    verbose: bool = False


@dataclass(frozen=True, slots=True)
class Stage:
    stage_id: str
    description: str
    handler: StageHandler
    aliases: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.stage_id, *self.aliases)


class StageRegistry:
    def __init__(self, stages: Iterable[Stage]) -> None:
        self._stages_by_id: dict[str, Stage] = {}
        self._lookup: dict[str, Stage] = {}
        for stage in stages:
            if stage.stage_id in self._stages_by_id:
                raise ValueError(f"duplicate stage id: {stage.stage_id}")
            self._stages_by_id[stage.stage_id] = stage
            for name in stage.all_names:
                key = self._normalize(name)
                if key in self._lookup:
                    other = self._lookup[key]
                    if other.stage_id == stage.stage_id:
                        continue
                    raise ValueError(f"duplicate stage alias '{name}' for {stage.stage_id} and {other.stage_id}")
                self._lookup[key] = stage

    @staticmethod
    def _normalize(name: str) -> str:
        return name.strip().lower().replace("-", "_")

    def list(self) -> list[Stage]:
        return list(self._stages_by_id.values())

    def resolve(self, name: str) -> Stage:
        key = self._normalize(name)
        try:
            return self._lookup[key]
        except KeyError as exc:
            raise KeyError(f"unknown stage: {name}") from exc

    def resolve_pipeline(self, requested: Iterable[str]) -> list[Stage]:
        ordered: list[Stage] = []
        seen: set[str] = set()

        def add_stage(stage: Stage) -> None:
            if stage.stage_id in seen:
                return
            for dep in stage.depends_on:
                add_stage(self.resolve(dep))
            ordered.append(stage)
            seen.add(stage.stage_id)

        for raw in requested:
            add_stage(self.resolve(raw))

        return ordered


def stage(
    stage_id: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
) -> Callable[[StageHandler], Stage]:
    """Decorator helper to declare a Stage in a concise way."""

    def decorator(func: StageHandler) -> Stage:
        return Stage(
            stage_id=stage_id,
            description=description,
            handler=func,
            aliases=aliases,
            depends_on=depends_on,
        )

    return decorator
