from __future__ import annotations

from dzsc.sdk import StageRegistry
from dzsc.stages_agentation import AGENTATION_STATUS_STAGE, INJECT_AGENTATION_STAGE, REMOVE_AGENTATION_STAGE
from dzsc.stages_frontend import DZ_SOURCE_MAPS_STAGE
from dzsc.stages_jit_agent_runtime import INJECT_JIT_AGENT_RUNTIME_STAGE, JIT_AGENT_RUNTIME_STATUS_STAGE
from dzsc.stages_schema import SCHEMA_CURRENT_STAGE, SCHEMA_LIST_STAGE, SCHEMA_SWITCH_STAGE


def build_registry() -> StageRegistry:
    return StageRegistry(
        [
            DZ_SOURCE_MAPS_STAGE,
            INJECT_AGENTATION_STAGE,
            REMOVE_AGENTATION_STAGE,
            AGENTATION_STATUS_STAGE,
            INJECT_JIT_AGENT_RUNTIME_STAGE,
            JIT_AGENT_RUNTIME_STATUS_STAGE,
            SCHEMA_LIST_STAGE,
            SCHEMA_CURRENT_STAGE,
            SCHEMA_SWITCH_STAGE,
        ]
    )
