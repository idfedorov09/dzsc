from __future__ import annotations

from dzsc.sdk import StageRegistry
from dzsc.stages_agentation import AGENTATION_STATUS_STAGE, INJECT_AGENTATION_STAGE, REMOVE_AGENTATION_STAGE
from dzsc.stages_frontend import DZ_SOURCE_MAPS_STAGE


def build_registry() -> StageRegistry:
    return StageRegistry(
        [
            DZ_SOURCE_MAPS_STAGE,
            INJECT_AGENTATION_STAGE,
            REMOVE_AGENTATION_STAGE,
            AGENTATION_STATUS_STAGE,
        ]
    )

