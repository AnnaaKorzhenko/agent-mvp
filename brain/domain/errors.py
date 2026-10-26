"""Anticipated domain failures. The MCP layer turns them into tool errors the model can read."""


class DomainError(Exception):
    pass


class NotFound(DomainError):
    pass


class InvalidInput(DomainError):
    pass
