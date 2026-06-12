from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from ...constants import SEPARATOR_DOT
from ...types_defs import NodeType
from . import relationships as rel

if TYPE_CHECKING:
    from ...services import IngestorProtocol
    from ...types_defs import FunctionRegistryTrieProtocol, SimpleNameLookup


def _common_prefix_len(qn: str, prefix: str) -> int:
    """Count the number of leading dot-separated segments shared by qn and prefix."""
    qn_parts = qn.split(SEPARATOR_DOT)
    prefix_parts = prefix.split(SEPARATOR_DOT)
    return sum(1 for a, b in zip(qn_parts, prefix_parts) if a == b)


def _resolve_kotlin_parent(
    parent_qn: str,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
) -> tuple[NodeType, str]:
    """Resolve a Kotlin parent QN to its canonical registry QN and NodeType.

    Kotlin parent qualified names from `extract_kotlin_supertypes` are
    package-rooted (e.g. `crossfile.Greeter`), but the function registry
    stores classes under file-path-rooted qns (e.g.
    `kotlin_cross.Zzz_iface.Greeter`). The Memgraph ingestor creates
    relationships via MATCH on both endpoints — if the target QN does not
    exist in the graph, the edge is silently dropped.

    Resolution order:
    1. Direct hit in function_registry — use as-is.
    2. Simple-name fallback via simple_name_lookup — prefer Interface
       candidates over Class candidates, and among same-type candidates
       prefer the one whose canonical QN shares the longest common path
       prefix with parent_qn. The prefix heuristic resolves the common
       case where the parent is in the same package: the fallback QN is
       `<child_module>.<simple_name>`, so a sibling file in the same
       directory shares more prefix segments than a class in an unrelated
       package. Alphabetical order is used as a stable tiebreaker.
    3. Return original QN unchanged (edge will likely be dropped by Memgraph,
       but we cannot do better without a full import-map re-routing pass).
    """
    direct = function_registry.get(parent_qn, None)
    if direct is not None:
        return direct, parent_qn  # type: ignore[return-value]

    simple_name = parent_qn.rsplit(SEPARATOR_DOT, 1)[-1]
    candidates = simple_name_lookup.get(simple_name, set())
    if not candidates:
        return NodeType.CLASS, parent_qn

    parent_module_prefix = parent_qn.rsplit(SEPARATOR_DOT, 1)[0]

    best_interface_qn: str | None = None
    best_interface_prefix_len = -1
    best_class_qn: str | None = None
    best_class_prefix_len = -1

    for candidate_qn in sorted(candidates):
        candidate_type = function_registry.get(candidate_qn, None)
        prefix_len = _common_prefix_len(candidate_qn, parent_module_prefix)
        if candidate_type == NodeType.INTERFACE:
            if prefix_len > best_interface_prefix_len:
                best_interface_prefix_len = prefix_len
                best_interface_qn = candidate_qn
        elif candidate_type == NodeType.CLASS:
            if prefix_len > best_class_prefix_len:
                best_class_prefix_len = prefix_len
                best_class_qn = candidate_qn

    if best_interface_qn is not None:
        iface_candidates = [c for c in sorted(candidates) if function_registry.get(c) == NodeType.INTERFACE]
        if len(iface_candidates) > 1 and best_interface_prefix_len == 0:
            logger.debug(
                "Ambiguous Kotlin parent '%s': multiple Interface candidates %s; "
                "picked '%s' by alphabetical fallback",
                parent_qn,
                iface_candidates,
                best_interface_qn,
            )
        return NodeType.INTERFACE, best_interface_qn

    if best_class_qn is not None:
        class_candidates = [c for c in sorted(candidates) if function_registry.get(c) == NodeType.CLASS]
        if len(class_candidates) > 1 and best_class_prefix_len == 0:
            logger.debug(
                "Ambiguous Kotlin parent '%s': multiple Class candidates %s; "
                "picked '%s' by alphabetical fallback",
                parent_qn,
                class_candidates,
                best_class_qn,
            )
        return NodeType.CLASS, best_class_qn

    return NodeType.CLASS, parent_qn


def process_all_kotlin_inheritance_edges(
    function_registry: FunctionRegistryTrieProtocol,
    pending_inheritance: dict[str, list[str]],
    simple_name_lookup: SimpleNameLookup,
    ingestor: IngestorProtocol,
) -> None:
    """Emit INHERITS / IMPLEMENTS edges for Kotlin classes in a deferred pass.

    Kotlin's grammar lumps class inheritance and interface implementation into
    a single `delegation_specifiers` block, so we cannot decide which edge type
    to emit while parsing each file. This pass runs after Pass 2 completes,
    by which point every parent's NodeType is stable in the function registry.
    """
    if not pending_inheritance:
        return

    logger.info("Resolving Kotlin inheritance / interface-implementation edges")

    for child_qn, parent_qns in pending_inheritance.items():
        child_node_type = function_registry.get(child_qn, NodeType.CLASS)
        for i, parent_qn in enumerate(parent_qns):
            parent_node_type, resolved_qn = _resolve_kotlin_parent(
                parent_qn, function_registry, simple_name_lookup
            )
            # parent_qns is the same list object as class_inheritance[child_qn]
            # (see relationships.create_class_relationships). Writing the
            # resolved canonical QN back here makes downstream passes —
            # notably process_all_method_overrides — see the registry-rooted
            # form and walk cross-file Kotlin inheritance correctly.
            if resolved_qn != parent_qn:
                parent_qns[i] = resolved_qn
            # IMPLEMENTS is reserved for non-interface child → Interface parent
            # (the schema rule is Class/Enum/Object → Interface). When an
            # Interface extends another Interface, that's INHERITS.
            if (
                parent_node_type == NodeType.INTERFACE
                and child_node_type != NodeType.INTERFACE
            ):
                rel.create_implements_relationship(
                    str(child_node_type), child_qn, resolved_qn, ingestor
                )
            else:
                rel.create_inheritance_relationship(
                    str(child_node_type),
                    child_qn,
                    resolved_qn,
                    function_registry,
                    ingestor,
                )
