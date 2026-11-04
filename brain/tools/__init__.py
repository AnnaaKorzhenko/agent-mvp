"""The brain's tools, independent of who calls them (docs/WEB_PLAN.md §2).

- base.py: the services, where a call comes from (Origin), and Call: resolves the user, remembers the message's
  signals, logs the call to the conversation.
- handlers.py: one handler per tool, `run(svc, name, origin, args)`.
- catalog.py: each tool's name, description and argument model, from the MCP signatures (the one place they're
  written), for callers other than MCP (our own chat).

brain/mcp_server/tools.py registers them as MCP tools; brain/web will call them in-process.
"""
