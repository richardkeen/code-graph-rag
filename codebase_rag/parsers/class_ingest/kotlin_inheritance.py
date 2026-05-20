from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from ...constants import SEPARATOR_DOT
from ...types_defs import NodeType
from . import relationships as rel

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol, SimpleNameLookup


def _resolve_kotlin_parent_type(
    parent_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
) -> NodeType:
    """Best-effort lookup of a Kotlin parent's NodeType.

    Kotlin parent qualified names from `extract_kotlin_supertypes` are
    package-rooted (e.g. `crossfile.Greeter`), but the function registry
    stores classes under file-path-rooted qns (e.g.
    `kotlin_cross.Zzz_iface.Greeter`). When the direct lookup misses, fall
    back to `simple_name_lookup` keyed by the trailing identifier, returning
    `NodeType.INTERFACE` if any candidate is an interface.
    """
    direct = function_registry.get(parent_qn, None)
    if direct is not None:
        return direct  # type: ignore[return-value]

    simple_name = parent_qn.rsplit(SEPARATOR_DOT, 1)[-1]
    candidates = simple_name_lookup.get(simple_name, set())
    for candidate_qn in candidates:
        candidate_type = function_registry.get(candidate_qn, None)
        if candidate_type == NodeType.INTERFACE:
            return NodeType.INTERFACE
    return NodeType.CLASS


def process_all_kotlin_inheritance_edges(
    function_registry: FunctionRegistryTrieProtocol,
    class_inheritance: dict[str, list[str]],
    kotlin_class_qns: set[str],
    simple_name_lookup: SimpleNameLookup,
    ingestor: IngestorProtocol,
) -> None:
    """Emit INHERITS / IMPLEMENTS edges for Kotlin classes in a deferred pass.

    Kotlin's grammar lumps class inheritance and interface implementation into
    a single `delegation_specifiers` block, so we cannot decide which edge type
    to emit while parsing each file. This pass runs after Pass 2 completes,
    by which point every parent's NodeType is stable in the function registry.
    """
    logger.info("Resolving Kotlin inheritance / interface-implementation edges")

    for child_qn, parent_qns in class_inheritance.items():
        if child_qn not in kotlin_class_qns:
            continue
        child_node_type = function_registry.get(child_qn, NodeType.CLASS)
        for parent_qn in parent_qns:
            parent_node_type = _resolve_kotlin_parent_type(
                parent_qn, function_registry, simple_name_lookup
            )
            if parent_node_type == NodeType.INTERFACE:
                rel.create_implements_relationship(
                    str(child_node_type), child_qn, parent_qn, ingestor
                )
            else:
                rel.create_inheritance_relationship(
                    str(child_node_type),
                    child_qn,
                    parent_qn,
                    function_registry,
                    ingestor,
                )
