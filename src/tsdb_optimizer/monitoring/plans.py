"""EXPLAIN plan parsing and stable plan signatures.

Only formats actually observed in ``tests/fixtures/plans`` are parsed:

* tree-model ``EXPLAIN`` uses ASCII boxes;
* tree-model ``EXPLAIN ANALYZE`` uses indented ``[PlanNodeId N]`` lines;
* the table model's EXPLAIN output was not verified, so it is never synthesized
  or guessed.  A non-empty table plan is reported ``UNAVAILABLE``.

The parser never executes SQL and never turns ``EXPLAIN`` into
``EXPLAIN ANALYZE``.  Callers are responsible for supplying text they already
obtained from an explicit diagnostic operation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ..schemas.monitoring import (
    PlanComparison,
    PlanComparisonStatus,
    PlanKind,
    PlanNode,
    PlanParseResult,
    PlanParseStatus,
    PlanSignature,
    PlanUnavailableReason,
)

_MODEL = Literal["tree", "table", "unknown"]


class PlanParseError(ValueError):
    """Raised internally when plan text cannot be parsed safely."""


@dataclass
class _Box:
    left: int
    right: int
    top: int
    bottom: int
    operator: str
    attributes: dict[str, str]
    ignored_attributes: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    parent: int | None = None
    children: list[int] = field(default_factory=list)


@dataclass
class _AnalyzeNode:
    operator: str
    indent: int
    parent: int | None = None
    children: list[int] = field(default_factory=list)


_CONNECTOR_CHARS = set("┌┐└┘┬┴├┤┼│─")
_UP_CONNECTORS = set("│├┤┼┬┴")
_TOP_EDGE = set("─┬┴┼")
_STABLE_ATTRIBUTES = {
    "Series",
    "QueryAllSensors",
    "ScanOrder",
    "Limit",
    "Order",
    "DeviceCount",
    "Aligned",
    "Offset",
}
_AGGREGATOR_ATTRIBUTE = re.compile(r"^Aggregator(?:-\d+)?$")
_ANALYZE_NODE = re.compile(
    r"^(?P<indent>[ \t]*)\[PlanNodeId\s+\d+\]:\s*"
    r"(?P<operator>[A-Za-z_][A-Za-z0-9_]*)"
    r"\((?P<operator_class>[^)]*)\)\s*$"
)
_ANALYZE_MARKERS = (
    "FRAGMENT-INSTANCE[",
    "[PlanNodeId ",
    "Analyze Cost:",
    "Total Wall Time:",
)


def _as_text(text: str | Sequence[str]) -> str:
    if isinstance(text, str):
        return text
    return "\n".join(str(line) for line in text)


def _normalize_operator(value: str) -> str:
    operator = value.strip()
    operator = re.sub(r"-\d+\s*$", "", operator)
    return operator.strip()


def _normalize_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _is_stable_attribute(name: str) -> bool:
    return name in _STABLE_ATTRIBUTES or bool(_AGGREGATOR_ATTRIBUTE.match(name))


def _line(text: str) -> str:
    return text.replace("\t", "    ")


def _box_candidates(line: str) -> list[tuple[int, int]]:
    """Return candidate ``(left, right)`` box-top spans on one line."""

    candidates: list[tuple[int, int]] = []
    start: int | None = None
    for position, char in enumerate(line):
        if char == "┌":
            start = position
        elif char == "┐" and start is not None:
            inner = line[start + 1 : position]
            if inner and all(item in _TOP_EDGE for item in inner):
                candidates.append((start, position))
            start = None
    return candidates


def _matching_bottom(lines: list[str], left: int, right: int, top: int) -> int | None:
    for row in range(top + 1, len(lines)):
        line = lines[row]
        if left < len(line) and right < len(line):
            if line[left] == "└" and line[right] == "┘":
                return row
    return None


def _extract_boxes(lines: list[str]) -> list[_Box]:
    boxes: list[_Box] = []
    occupied_tops: set[tuple[int, int]] = set()
    for row, raw_line in enumerate(lines):
        line = _line(raw_line)
        for left, right in _box_candidates(line):
            if (row, left) in occupied_tops:
                continue
            bottom = _matching_bottom(lines, left, right, row)
            if bottom is None:
                continue
            occupied_tops.add((row, left))
            content: list[str] = []
            for content_row in range(row + 1, bottom):
                source = _line(lines[content_row])
                if left + 1 <= len(source):
                    segment = source[left + 1 : right].strip()
                    if segment:
                        content.append(segment)
                elif left + 1 < right:
                    # A short line carries no text inside this box.
                    continue
            if not content:
                raise PlanParseError(f"box at row {row} has no operator header")
            header = _normalize_operator(content[0])
            if not header:
                raise PlanParseError(f"box at row {row} has an empty operator header")
            attributes: dict[str, str] = {}
            ignored: set[str] = set()
            warnings: list[str] = []
            for segment in content[1:]:
                if ":" not in segment:
                    warnings.append(f"ignored non-attribute plan line: {segment!r}")
                    continue
                key, value = segment.split(":", 1)
                key = key.strip()
                value = _normalize_value(value)
                if not key:
                    warnings.append(f"ignored attribute with empty name: {segment!r}")
                    continue
                if not _is_stable_attribute(key):
                    ignored.add(key)
                    continue
                if key in attributes:
                    warnings.append(f"duplicate stable attribute {key!r}; last value kept")
                attributes[key] = value
            boxes.append(
                _Box(
                    left=left,
                    right=right,
                    top=row,
                    bottom=bottom,
                    operator=header,
                    attributes=attributes,
                    ignored_attributes=ignored,
                    warnings=warnings,
                )
            )
    boxes.sort(key=lambda box: (box.top, box.left))
    return boxes


def _box_at_bottom(boxes: Sequence[_Box], row: int, column: int) -> int | None:
    matches = [
        index
        for index, box in enumerate(boxes)
        if box.bottom == row and box.left <= column <= box.right
    ]
    if not matches:
        return None
    return min(matches, key=lambda index: boxes[index].right - boxes[index].left)


def _find_parent(boxes: list[_Box], index: int, lines: list[str]) -> int | None:
    box = boxes[index]
    if box.top <= 0:
        return None

    # Sometimes the parent bottom border is immediately above the child.
    direct = _box_at_bottom(boxes, box.top - 1, (box.left + box.right) // 2)
    if direct is not None and direct != index and boxes[direct].top < box.top:
        return direct

    start_row = box.top - 1
    start_line = _line(lines[start_row]) if start_row < len(lines) else ""
    starts = [
        column
        for column in range(box.left, box.right + 1)
        if column < len(start_line) and start_line[column] in _UP_CONNECTORS
    ]
    if not starts:
        return None

    roots = [(start_row, column) for column in starts]
    queue: deque[tuple[int, int]] = deque(roots)
    visited: set[tuple[int, int]] = set()
    candidates: list[tuple[int, int, int]] = []  # (row, distance, box index)
    origin = (box.left + box.right) / 2.0

    while queue:
        row, column = queue.popleft()
        if (row, column) in visited:
            continue
        visited.add((row, column))
        if row < 0 or column < 0:
            continue
        line = _line(lines[row]) if row < len(lines) else ""
        if column >= len(line):
            continue
        char = line[column]
        if char not in _CONNECTOR_CHARS:
            continue

        parent = _box_at_bottom(boxes, row, column)
        if (
            parent is not None
            and parent != index
            and boxes[parent].top < box.top
        ):
            candidates.append((row, abs(column - origin), parent))
            # Do not enter the interior of the parent box.
            continue

        for next_row, next_column in (
            (row - 1, column),
            (row, column - 1),
            (row, column + 1),
        ):
            if next_row < 0 or next_column < 0:
                continue
            next_line = _line(lines[next_row]) if next_row < len(lines) else ""
            if next_column >= len(next_line):
                continue
            if next_line[next_column] in _CONNECTOR_CHARS:
                queue.append((next_row, next_column))

    if not candidates:
        return None
    # Prefer the closest level above; then the closest connector horizontally.
    best_row = max(candidate[0] for candidate in candidates)
    same_level = [candidate for candidate in candidates if candidate[0] == best_row]
    return min(same_level, key=lambda candidate: candidate[1])[2]


def _attach_box_tree(boxes: list[_Box], lines: list[str]) -> list[int]:
    for index, box in enumerate(boxes):
        parent = _find_parent(boxes, index, lines)
        box.parent = parent
        if parent is not None:
            boxes[parent].children.append(index)
    return [index for index, box in enumerate(boxes) if box.parent is None]


def _canonical_nodes(
    *,
    operators: Sequence[str],
    attributes: Sequence[dict[str, str]],
    parents: Sequence[int | None],
    children: Sequence[list[int]],
) -> list[PlanNode]:
    """Order nodes deterministically and recompute parent indices."""

    node_count = len(operators)
    if node_count == 0:
        raise PlanParseError("no plan nodes")
    if len(attributes) != node_count or len(parents) != node_count:
        raise PlanParseError("inconsistent plan node arrays")

    def subtree_key(index: int, seen: frozenset[int]) -> tuple[Any, ...]:
        if index in seen:
            raise PlanParseError("cycle in plan node tree")
        next_seen = seen | {index}
        child_keys = tuple(
            sorted(subtree_key(child, next_seen) for child in children[index])
        )
        return (
            operators[index],
            tuple(sorted(attributes[index].items())),
            child_keys,
        )

    roots = [index for index, parent in enumerate(parents) if parent is None]
    if not roots:
        raise PlanParseError("plan has no root node")
    ordered: list[tuple[int, int | None, int]] = []
    visited: set[int] = set()

    def visit(old_index: int, parent_new: int | None, depth: int) -> None:
        if old_index in visited:
            raise PlanParseError("plan node appears more than once")
        visited.add(old_index)
        new_index = len(ordered)
        ordered.append((old_index, parent_new, depth))
        for child in sorted(
            children[old_index],
            key=lambda child_index: subtree_key(child_index, frozenset()),
        ):
            visit(child, new_index, depth + 1)

    for root in sorted(roots, key=lambda root: subtree_key(root, frozenset())):
        visit(root, None, 0)
    if len(visited) != node_count:
        raise PlanParseError("plan contains unreachable nodes")

    return [
        PlanNode(
            operator=operators[old_index],
            attributes=dict(attributes[old_index]),
            parent_index=parent_new,
            depth=depth,
        )
        for old_index, parent_new, depth in ordered
    ]


def _hash_signature(signature: PlanSignature) -> str:
    canonical = json.dumps(
        signature.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stable_plan_hash(signature: PlanSignature) -> str:
    return _hash_signature(signature)


def _empty_result(
    *,
    model: _MODEL,
    kind: PlanKind,
    raw_line_count: int,
    warnings: Sequence[str] = (),
) -> PlanParseResult:
    return PlanParseResult(
        status=PlanParseStatus.EMPTY,
        model=model,
        plan_kind=kind,
        raw_line_count=raw_line_count,
        warnings=list(warnings),
    )


def _unavailable_result(
    *,
    model: _MODEL,
    kind: PlanKind,
    reason: PlanUnavailableReason,
    raw_line_count: int,
    warnings: Sequence[str] = (),
) -> PlanParseResult:
    return PlanParseResult(
        status=PlanParseStatus.UNAVAILABLE,
        model=model,
        plan_kind=kind,
        raw_line_count=raw_line_count,
        unavailable_reason=reason,
        warnings=list(warnings),
    )


def _error_result(
    *,
    model: _MODEL,
    kind: PlanKind,
    raw_line_count: int,
    error_type: str,
    error_message: str,
    warnings: Sequence[str] = (),
) -> PlanParseResult:
    return PlanParseResult(
        status=PlanParseStatus.ERROR,
        model=model,
        plan_kind=kind,
        raw_line_count=raw_line_count,
        warnings=list(warnings),
        error_type=error_type,
        error_message=error_message,
    )


def _available_result(
    *,
    model: Literal["tree", "table"],
    kind: PlanKind,
    nodes: list[PlanNode],
    raw_line_count: int,
    excluded_attributes: set[str],
    warnings: Sequence[str],
) -> PlanParseResult:
    signature = PlanSignature(
        model=model,
        plan_kind=kind,
        nodes=nodes,
    )
    return PlanParseResult(
        status=PlanParseStatus.AVAILABLE,
        model=model,
        plan_kind=kind,
        signature=signature,
        signature_hash=_hash_signature(signature),
        node_count=len(nodes),
        raw_line_count=raw_line_count,
        excluded_attributes=sorted(excluded_attributes),
        warnings=list(warnings),
    )


class TreePlanParser:
    """Parse the tree-model plan formats verified in Phase 0."""

    model: Literal["tree"] = "tree"

    def parse(
        self,
        text: str | Sequence[str],
        *,
        kind: PlanKind = PlanKind.UNKNOWN,
    ) -> PlanParseResult:
        normalized = _as_text(text)
        raw_line_count = len(normalized.splitlines()) if normalized else 0
        if not normalized.strip():
            return _empty_result(
                model="tree",
                kind=kind if kind is not PlanKind.UNKNOWN else PlanKind.EXPLAIN,
                raw_line_count=raw_line_count,
            )

        contains_analyze = any(marker in normalized for marker in _ANALYZE_MARKERS)
        effective_kind = kind
        if kind is PlanKind.UNKNOWN:
            effective_kind = (
                PlanKind.EXPLAIN_ANALYZE if contains_analyze else PlanKind.EXPLAIN
            )
        elif kind is PlanKind.EXPLAIN and contains_analyze:
            return _error_result(
                model="tree",
                kind=kind,
                raw_line_count=raw_line_count,
                error_type="PlanKindMismatch",
                error_message=(
                    "EXPLAIN ANALYZE markers were found in text supplied as EXPLAIN"
                ),
            )
        elif kind is PlanKind.EXPLAIN_ANALYZE and not contains_analyze:
            return _error_result(
                model="tree",
                kind=kind,
                raw_line_count=raw_line_count,
                error_type="PlanKindMismatch",
                error_message=(
                    "EXPLAIN ANALYZE was requested but the runtime markers are absent"
                ),
            )

        try:
            if effective_kind is PlanKind.EXPLAIN_ANALYZE:
                nodes, excluded, warnings = self._parse_analyze(normalized)
            else:
                nodes, excluded, warnings = self._parse_boxes(normalized)
        except PlanParseError as exc:
            return _error_result(
                model="tree",
                kind=effective_kind,
                raw_line_count=raw_line_count,
                error_type=type(exc).__name__,
                error_message=str(exc)[:4000],
            )
        except Exception as exc:  # noqa: BLE001 - parser must return a status
            return _error_result(
                model="tree",
                kind=effective_kind,
                raw_line_count=raw_line_count,
                error_type=type(exc).__name__,
                error_message=str(exc)[:4000],
            )

        if not nodes:
            return _unavailable_result(
                model="tree",
                kind=effective_kind,
                reason=PlanUnavailableReason.NO_RECOGNIZED_PLAN_NODES,
                raw_line_count=raw_line_count,
                warnings=warnings,
            )
        return _available_result(
            model="tree",
            kind=effective_kind,
            nodes=nodes,
            raw_line_count=raw_line_count,
            excluded_attributes=excluded,
            warnings=warnings,
        )

    def _parse_boxes(
        self, text: str
    ) -> tuple[list[PlanNode], set[str], list[str]]:
        lines = text.splitlines()
        boxes = _extract_boxes(lines)
        if not boxes:
            return [], set(), ["no trusted ASCII plan boxes were recognized"]
        roots = _attach_box_tree(boxes, lines)
        if not roots:
            raise PlanParseError("parsed boxes have no root node")

        operators = [box.operator for box in boxes]
        attributes = [box.attributes for box in boxes]
        parents = [box.parent for box in boxes]
        children = [box.children for box in boxes]
        nodes = _canonical_nodes(
            operators=operators,
            attributes=attributes,
            parents=parents,
            children=children,
        )
        excluded: set[str] = set()
        warnings: list[str] = []
        for box in boxes:
            excluded.update(box.ignored_attributes)
            warnings.extend(box.warnings)
        return nodes, excluded, warnings

    def _parse_analyze(
        self, text: str
    ) -> tuple[list[PlanNode], set[str], list[str]]:
        analyze_nodes: list[_AnalyzeNode] = []
        warnings: list[str] = []
        stack: list[tuple[int, int]] = []  # (indent, node index)
        for line in text.splitlines():
            match = _ANALYZE_NODE.match(line)
            if match is None:
                continue
            operator = _normalize_operator(match.group("operator"))
            if not operator:
                raise PlanParseError("EXPLAIN ANALYZE node has an empty operator")
            indent = len(match.group("indent"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else None
            index = len(analyze_nodes)
            analyze_nodes.append(
                _AnalyzeNode(operator=operator, indent=indent, parent=parent)
            )
            if parent is not None:
                analyze_nodes[parent].children.append(index)
            stack.append((indent, index))
            operator_class = match.group("operator_class").strip()
            if operator_class and "(" in operator_class:
                warnings.append(
                    f"operator class for {operator} was treated as volatile metadata"
                )

        if not analyze_nodes:
            return [], {"PlanNodeId"}, warnings
        nodes = _canonical_nodes(
            operators=[node.operator for node in analyze_nodes],
            attributes=[{} for _ in analyze_nodes],
            parents=[node.parent for node in analyze_nodes],
            children=[node.children for node in analyze_nodes],
        )
        return (
            nodes,
            {
                "PlanNodeId",
                "OperatorClass",
                "CPU Time",
                "Wall Time",
                "Output",
                "Memory",
                "FileCount",
                "FragmentInstance",
                "DataRegion",
                "Address",
                "Cost",
            },
            warnings,
        )


class TablePlanParser:
    """Fail closed until a real table-model EXPLAIN fixture exists.

    The audited 2.0.6 environment had no user table-model tables, so there is no
    verified table EXPLAIN schema and no legitimate node structure to hash.
    """

    model: Literal["table"] = "table"

    def parse(
        self,
        text: str | Sequence[str],
        *,
        kind: PlanKind = PlanKind.UNKNOWN,
    ) -> PlanParseResult:
        normalized = _as_text(text)
        raw_line_count = len(normalized.splitlines()) if normalized else 0
        effective_kind = kind if kind is not PlanKind.UNKNOWN else PlanKind.EXPLAIN
        if not normalized.strip():
            return _empty_result(
                model="table", kind=effective_kind, raw_line_count=raw_line_count
            )
        return _unavailable_result(
            model="table",
            kind=effective_kind,
            reason=PlanUnavailableReason.TABLE_MODEL_EXPLAIN_NOT_VERIFIED,
            raw_line_count=raw_line_count,
            warnings=[
                "table-model EXPLAIN output has no verified Phase-0 fixture; "
                "no node structure or plan hash was fabricated"
            ],
        )


class PlanParser:
    """Dispatch EXPLAIN parsing by verified database model."""

    def __init__(
        self,
        *,
        tree_parser: TreePlanParser | None = None,
        table_parser: TablePlanParser | None = None,
    ):
        self.tree_parser = tree_parser or TreePlanParser()
        self.table_parser = table_parser or TablePlanParser()

    def parse(
        self,
        text: str | Sequence[str],
        *,
        model: str,
        kind: PlanKind = PlanKind.UNKNOWN,
    ) -> PlanParseResult:
        normalized_model = model.strip().lower()
        if normalized_model == "tree":
            return self.tree_parser.parse(text, kind=kind)
        if normalized_model == "table":
            return self.table_parser.parse(text, kind=kind)
        normalized = _as_text(text)
        raw_line_count = len(normalized.splitlines()) if normalized else 0
        return _unavailable_result(
            model="unknown",
            kind=kind if kind is not PlanKind.UNKNOWN else PlanKind.UNKNOWN,
            reason=PlanUnavailableReason.UNKNOWN_DATABASE_MODEL,
            raw_line_count=raw_line_count,
            warnings=[f"database model {model!r} has no verified EXPLAIN parser"],
        )

    def parse_rows(
        self,
        rows: Sequence[Sequence[Any]],
        *,
        model: str,
        kind: PlanKind = PlanKind.UNKNOWN,
    ) -> PlanParseResult:
        """Parse EXPLAIN result rows; each row must contain exactly one cell."""

        try:
            lines: list[str] = []
            for row_index, row in enumerate(rows):
                if len(row) != 1:
                    raise PlanParseError(
                        f"EXPLAIN row {row_index} has {len(row)} columns; expected 1"
                    )
                value = row[0]
                if isinstance(value, (bytes, bytearray)):
                    value = bytes(value).decode("utf-8", errors="replace")
                if not isinstance(value, str):
                    raise PlanParseError(
                        f"EXPLAIN row {row_index} is not text: {type(value).__name__}"
                    )
                lines.extend(value.splitlines() or [""])
        except PlanParseError as exc:
            return _error_result(
                model=model if model in {"tree", "table"} else "unknown",
                kind=kind if kind is not PlanKind.UNKNOWN else PlanKind.UNKNOWN,
                raw_line_count=len(rows),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return self.parse("\n".join(lines), model=model, kind=kind)


def compare_plan_results(
    expected: PlanParseResult,
    actual: PlanParseResult,
) -> PlanComparison:
    """Compare only stable hashes; never compare raw plan text."""

    if (
        expected.status is not PlanParseStatus.AVAILABLE
        or actual.status is not PlanParseStatus.AVAILABLE
        or expected.signature_hash is None
        or actual.signature_hash is None
    ):
        return PlanComparison(
            status=PlanComparisonStatus.UNAVAILABLE,
            expected_hash=expected.signature_hash,
            actual_hash=actual.signature_hash,
            reason=(
                f"expected={expected.status.value}, actual={actual.status.value}; "
                "a stable hash is required for comparison"
            ),
        )
    if expected.signature_hash == actual.signature_hash:
        return PlanComparison(
            status=PlanComparisonStatus.MATCHED,
            expected_hash=expected.signature_hash,
            actual_hash=actual.signature_hash,
        )
    return PlanComparison(
        status=PlanComparisonStatus.MISMATCHED,
        expected_hash=expected.signature_hash,
        actual_hash=actual.signature_hash,
        reason="stable plan signatures differ",
    )


def compare_plan_hashes(
    expected: PlanParseResult | str | None,
    actual: PlanParseResult | str | None,
) -> PlanComparison:
    """Convenience wrapper accepting plan-parse results or raw SHA-256 hashes."""

    def hash_of(value: PlanParseResult | str | None) -> str | None:
        if isinstance(value, PlanParseResult):
            return value.signature_hash
        return value

    expected_hash = hash_of(expected)
    actual_hash = hash_of(actual)
    if expected_hash is None or actual_hash is None:
        return PlanComparison(
            status=PlanComparisonStatus.UNAVAILABLE,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            reason="a stable plan hash is unavailable on at least one side",
        )
    if expected_hash == actual_hash:
        return PlanComparison(
            status=PlanComparisonStatus.MATCHED,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
        )
    return PlanComparison(
        status=PlanComparisonStatus.MISMATCHED,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        reason="stable plan signatures differ",
    )


def parse_plan_text(
    text: str | Sequence[str],
    *,
    model: str,
    kind: PlanKind = PlanKind.UNKNOWN,
) -> PlanParseResult:
    return PlanParser().parse(text, model=model, kind=kind)
