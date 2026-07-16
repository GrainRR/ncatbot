"""Todo 工具的唯一注册表。"""

from __future__ import annotations

from . import create_tools, merge_tools, query_tools, state_tools, update_tools
from .contracts import ToolSpec


TOOL_ORDER = (
    "list_todos",
    "get_todo",
    "create_todo",
    "edit_todo",
    "shift_todo_time",
    "complete_todos",
    "cancel_todo",
    "restore_todos",
    "delete_todos",
    "merge_todos",
)


def _build_registry() -> tuple[ToolSpec, ...]:
    specs = (
        *query_tools.tool_specs(),
        *create_tools.tool_specs(),
        *update_tools.tool_specs(),
        *state_tools.tool_specs(),
        *merge_tools.tool_specs(),
    )
    names = tuple(spec.name for spec in specs)
    if len(set(names)) != len(names):
        raise RuntimeError(f"duplicate todo tool names: {names}")
    if names != TOOL_ORDER:
        raise RuntimeError(f"unexpected todo tool order: {names}")
    return specs


TOOL_SPECS = _build_registry()
TOOL_SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}
TODO_TOOL_NAMES = tuple(spec.name for spec in TOOL_SPECS)


def openai_tool_definitions() -> list[dict[str, object]]:
    """从唯一注册表派生 OpenAI-compatible 工具定义。"""

    return [spec.to_openai_tool() for spec in TOOL_SPECS]
