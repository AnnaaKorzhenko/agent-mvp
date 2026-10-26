"""Reads docs/DATA_CONTRACT.md and checks documents against it.

The markdown tables are the single source of truth. Used by the contract test, and by
repositories to log a clear error when a required field is missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from bson import ObjectId

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "DATA_CONTRACT.md"

_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,), "int": (int,), "float": (float, int), "bool": (bool,), "datetime": (datetime,),
    "objectid": (ObjectId,), "list": (list,), "dict": (dict,), "any": (object,),
}
_MISSING = object()


@dataclass(frozen=True)
class FieldRule:
    collection: str
    path: str
    types: tuple[str, ...]
    nullable: bool
    required: bool
    access: str


@dataclass(frozen=True)
class Problem:
    collection: str
    doc_id: str
    path: str
    kind: str        # "missing" | "type"
    detail: str = ""


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, list[FieldRule]]:
    """{collection: [FieldRule]} from the '## <collection>' tables."""
    rules: dict[str, list[FieldRule]] = {}
    collection = None
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(\w+)", line)
        if heading:
            collection = heading.group(1)
            continue
        # Columns are split on unescaped pipes; "\|" inside a cell is a literal "|" (e.g. "int\|objectid").
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if not collection or len(cells) < 4 or not cells[0].startswith("`"):
            continue
        field, type_spec, required, access = cells[0].strip("`"), cells[1], cells[2], cells[3]
        nullable = type_spec.endswith("?")
        types = tuple(type_spec.rstrip("?").split("|"))
        unknown = [t for t in types if t not in _TYPES]
        if unknown:
            raise ValueError(f"DATA_CONTRACT.md: unknown type {unknown} for {collection}.{field}")
        rules.setdefault(collection, []).append(
            FieldRule(collection, field, types, nullable, required == "yes", access))
    return rules


def _values(doc: Any, parts: list[str]) -> Iterator[Any]:
    """Every value at a dotted path; 'items[]' walks each list element. Yields _MISSING where absent."""
    if not parts:
        yield doc
        return
    head, rest = parts[0], parts[1:]
    each = head.endswith("[]")
    key = head[:-2] if each else head
    if not isinstance(doc, dict) or key not in doc:
        yield _MISSING
        return
    value = doc[key]
    if each:
        if not isinstance(value, list):
            yield _MISSING
            return
        for element in value:
            yield from _values(element, rest)
    else:
        yield from _values(value, rest)


def _parent_present(doc: dict, path: str) -> bool:
    """A required nested field is checked only where its optional parent exists."""
    parts = path.split(".")
    if len(parts) == 1:
        return True
    return any(v is not _MISSING for v in _values(doc, parts[:-1]))


def check_document(doc: dict, rules: list[FieldRule]) -> list[Problem]:
    problems = []
    doc_id = str(doc.get("_id", "?"))
    for rule in rules:
        if not _parent_present(doc, rule.path):
            continue
        for value in _values(doc, rule.path.split(".")):
            if value is _MISSING:
                if rule.required:
                    problems.append(Problem(rule.collection, doc_id, rule.path, "missing"))
                continue
            if value is None:
                if not rule.nullable:
                    problems.append(Problem(rule.collection, doc_id, rule.path, "type", "null"))
                continue
            allowed = tuple(t for name in rule.types for t in _TYPES[name])
            if isinstance(value, bool) and bool not in allowed:      # bool is an int subclass
                ok = False
            else:
                ok = isinstance(value, allowed)
            if not ok:
                problems.append(Problem(rule.collection, doc_id, rule.path, "type", type(value).__name__))
    return problems


def required_paths(collection: str, rules: dict[str, list[FieldRule]] | None = None) -> list[str]:
    rules = rules if rules is not None else load_contract()
    return [r.path for r in rules.get(collection, []) if r.required and "[]" not in r.path and "." not in r.path]
