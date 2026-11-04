"""Each tool's name, description and arguments for callers other than MCP (our web chat, docs/WEB_PLAN.md §2).

The MCP signatures in brain/mcp_server/tools.py are the one place a tool's arguments and description are
written; the SDK turns them into a pydantic model and a JSON schema, and so do we here. For our own chat the
schema leaves out user_message and signals: the chat has the user's message itself, and the learner reads it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from brain.tools.base import Origin, Services
from brain.tools.handlers import OWN, run


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str | None
    description: str
    annotations: ToolAnnotations | None
    args_model: type[BaseModel]       # every argument, user_message and signals included
    input_schema: dict                # the JSON schema without user_message and signals

    @property
    def read_only(self) -> bool:
        return bool(self.annotations and self.annotations.read_only_hint)

    def arguments(self, args: dict, user_message: str) -> dict:
        """The model's arguments validated (pydantic's ValidationError if they don't fit), with the message."""
        model = self.args_model.model_validate({**args, "user_message": user_message})
        return {k: getattr(model, k) for k in type(model).model_fields}


def spec_of(fn, **kw: Any) -> ToolSpec:
    tool = Tool.from_function(fn, title=kw.get("title"), annotations=kw.get("annotations"))
    schema = dict(tool.parameters)
    schema["properties"] = {k: v for k, v in schema.get("properties", {}).items() if k not in OWN}
    schema["required"] = [k for k in schema.get("required", []) if k not in OWN]
    used = json.dumps(schema["properties"])
    defs = {k: v for k, v in schema.pop("$defs", {}).items() if f'"#/$defs/{k}"' in used}
    if defs:
        schema["$defs"] = defs
    return ToolSpec(name=tool.name, title=tool.title, description=tool.description, annotations=tool.annotations,
                    args_model=tool.fn_metadata.arg_model, input_schema=schema)


def build(svc: Services) -> dict[str, ToolSpec]:
    """The specs of every tool, without an MCP server to serve them."""
    from mcp.server import MCPServer

    from brain.mcp_server.tools import register_tools
    return register_tools(MCPServer("catalog"), svc)


async def call(svc: Services, spec: ToolSpec, args: dict, user_message: str, origin: Origin | None = None):
    """Runs a tool the way MCP would: arguments validated against its signature, then its handler."""
    return await run(svc, spec.name, origin, spec.arguments(args, user_message))
