from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
    get_relationships,
)
from codebase_rag.types_defs import NodeType


def _edge_targets(relationships: list, child_qn_suffix: str) -> set[str]:
    """Return the set of target qualified names for edges whose source qn ends with the given suffix."""
    targets: set[str] = set()
    for call in relationships:
        src = call.args[0]
        tgt = call.args[2]
        if src[2].endswith(child_qn_suffix):
            targets.add(tgt[2])
    return targets


@pytest.fixture
def kotlin_interfaces_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_interfaces"
    project_path.mkdir()
    (project_path / "Same.kt").write_text(
        encoding="utf-8",
        data="""
package shapes

interface Greeter {
    fun greet(): String
}

open class Animal(val name: String) {
    open fun sound(): String = "generic"
}

class Plain(val id: Int)

class Foo : Greeter {
    override fun greet(): String = "hi"
}

class Bar : Animal("bar")

class Both : Animal("both"), Greeter {
    override fun greet(): String = "hey"
}
""",
    )
    return project_path


@pytest.fixture
def kotlin_cross_file_project(temp_repo: Path) -> Path:
    """Interface declared in one file, implementer in another, with names chosen
    so the implementer's file sorts BEFORE the interface's file (alphabetical
    Path iteration). This is the case the deferred-resolution pass exists for —
    at the moment ClientImpl is processed, Greeter has not yet been ingested.
    """
    project_path = temp_repo / "kotlin_cross"
    project_path.mkdir()
    (project_path / "Aaa_client.kt").write_text(
        encoding="utf-8",
        data="""
package crossfile

import crossfile.Greeter

class ClientImpl : Greeter {
    override fun greet(): String = "hello"
}
""",
    )
    (project_path / "Zzz_iface.kt").write_text(
        encoding="utf-8",
        data="""
package crossfile

interface Greeter {
    fun greet(): String
}
""",
    )
    return project_path


def test_kotlin_interface_node_label(
    kotlin_interfaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_interfaces_project, mock_ingestor, skip_if_missing="kotlin"
    )

    interfaces = get_node_names(mock_ingestor, NodeType.INTERFACE)
    classes = get_node_names(mock_ingestor, NodeType.CLASS)

    assert any(name.endswith("Greeter") for name in interfaces), interfaces
    assert any(name.endswith("Plain") for name in classes), classes
    assert any(name.endswith("Animal") for name in classes), classes
    # Greeter must NOT be a Class node.
    assert not any(name.endswith("Greeter") for name in classes), classes


def test_kotlin_implements_edge_for_interface_parent(
    kotlin_interfaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_interfaces_project, mock_ingestor, skip_if_missing="kotlin"
    )

    implements = get_relationships(mock_ingestor, "IMPLEMENTS")
    inherits = get_relationships(mock_ingestor, "INHERITS")

    ingested_interfaces = get_node_names(mock_ingestor, NodeType.INTERFACE)

    foo_implements_targets = _edge_targets(implements, "Foo")
    assert any(t in ingested_interfaces for t in foo_implements_targets), (
        f"Foo should IMPLEMENTS an ingested Interface node, got {foo_implements_targets}, "
        f"ingested interfaces={ingested_interfaces}"
    )
    foo_inherits_targets = _edge_targets(inherits, "Foo")
    assert not any(t in ingested_interfaces for t in foo_inherits_targets), (
        f"Foo should not INHERITS an Interface node, got {foo_inherits_targets}"
    )


def test_kotlin_inherits_edge_for_class_parent(
    kotlin_interfaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_interfaces_project, mock_ingestor, skip_if_missing="kotlin"
    )

    inherits = get_relationships(mock_ingestor, "INHERITS")
    implements = get_relationships(mock_ingestor, "IMPLEMENTS")

    ingested_classes = get_node_names(mock_ingestor, NodeType.CLASS)

    bar_inherits_targets = _edge_targets(inherits, "Bar")
    assert any(t in ingested_classes for t in bar_inherits_targets), (
        f"Bar should INHERITS an ingested Class node, got {bar_inherits_targets}, "
        f"ingested classes={ingested_classes}"
    )
    bar_implements_targets = _edge_targets(implements, "Bar")
    assert not any(t in ingested_classes for t in bar_implements_targets), (
        f"Bar should not IMPLEMENTS a Class node, got {bar_implements_targets}"
    )


def test_kotlin_mixed_extends_and_implements(
    kotlin_interfaces_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_interfaces_project, mock_ingestor, skip_if_missing="kotlin"
    )

    inherits = get_relationships(mock_ingestor, "INHERITS")
    implements = get_relationships(mock_ingestor, "IMPLEMENTS")

    ingested_interfaces = get_node_names(mock_ingestor, NodeType.INTERFACE)
    ingested_classes = get_node_names(mock_ingestor, NodeType.CLASS)

    both_inherits_targets = _edge_targets(inherits, "Both")
    both_implements_targets = _edge_targets(implements, "Both")

    assert any(t in ingested_classes for t in both_inherits_targets), (
        f"Both should INHERITS an ingested Class node, got {both_inherits_targets}"
    )
    assert any(t in ingested_interfaces for t in both_implements_targets), (
        f"Both should IMPLEMENTS an ingested Interface node, got {both_implements_targets}"
    )


@pytest.fixture
def kotlin_interface_extends_interface_project(temp_repo: Path) -> Path:
    """A Kotlin interface extending another interface. The deferred resolution
    pass must emit INHERITS (interface→interface), NOT IMPLEMENTS, since
    IMPLEMENTS is reserved for non-interface implementers in the schema.
    """
    project_path = temp_repo / "kotlin_iface_chain"
    project_path.mkdir()
    (project_path / "Same.kt").write_text(
        encoding="utf-8",
        data="""
package shapes

interface Base {
    fun base(): String
}

interface Derived : Base {
    fun derived(): String
}

class Impl : Derived {
    override fun base(): String = "b"
    override fun derived(): String = "d"
}
""",
    )
    return project_path


def test_kotlin_interface_extending_interface_emits_inherits(
    kotlin_interface_extends_interface_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_interface_extends_interface_project,
        mock_ingestor,
        skip_if_missing="kotlin",
    )

    inherits = get_relationships(mock_ingestor, "INHERITS")
    implements = get_relationships(mock_ingestor, "IMPLEMENTS")

    ingested_interfaces = get_node_names(mock_ingestor, NodeType.INTERFACE)

    derived_inherits_targets = _edge_targets(inherits, "Derived")
    derived_implements_targets = _edge_targets(implements, "Derived")

    # Derived (Interface) extending Base (Interface) must be INHERITS.
    assert any(t in ingested_interfaces for t in derived_inherits_targets), (
        f"Derived should INHERITS Base (Interface→Interface), got "
        f"inherits={derived_inherits_targets}, interfaces={ingested_interfaces}"
    )
    # And must NOT emit IMPLEMENTS — that edge type is reserved for
    # non-interface child → interface parent.
    assert not derived_implements_targets, (
        f"Interface extending Interface must not emit IMPLEMENTS, got "
        f"{derived_implements_targets}"
    )

    # Sanity: the class that ultimately implements the chain still uses
    # IMPLEMENTS for its direct interface parent.
    impl_implements_targets = _edge_targets(implements, "Impl")
    assert any(t in ingested_interfaces for t in impl_implements_targets), (
        f"Impl should still IMPLEMENTS Derived, got {impl_implements_targets}"
    )


def test_kotlin_cross_file_implements_resolves_correctly(
    kotlin_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression guard: the implementer's file is processed before the
    interface's file (alphabetical order), so synchronous registry lookups
    would mis-classify this as INHERITS. The deferred Kotlin pass is what
    makes this test pass."""
    create_and_run_updater(
        kotlin_cross_file_project, mock_ingestor, skip_if_missing="kotlin"
    )

    implements = get_relationships(mock_ingestor, "IMPLEMENTS")
    inherits = get_relationships(mock_ingestor, "INHERITS")

    ingested_interfaces = get_node_names(mock_ingestor, NodeType.INTERFACE)

    client_implements_targets = _edge_targets(implements, "ClientImpl")
    client_inherits_targets = _edge_targets(inherits, "ClientImpl")

    assert any(t in ingested_interfaces for t in client_implements_targets), (
        f"ClientImpl should IMPLEMENTS an ingested Interface node (cross-file), got "
        f"implements={client_implements_targets}, interfaces={ingested_interfaces}"
    )
    assert not any(t in ingested_interfaces for t in client_inherits_targets), (
        f"ClientImpl should not INHERITS an Interface node, got {client_inherits_targets}"
    )


def test_kotlin_cross_file_override_emits_overrides_edge(
    kotlin_cross_file_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Cross-file Kotlin overrides resolve through the deferred pass.

    Regression guard for the bug where pending_inheritance kept the
    package-rooted parent QN (e.g. `crossfile.Greeter`) after the deferred
    pass resolved IMPLEMENTS edges, leaving the subsequent
    process_all_method_overrides pass unable to find the canonical Greeter
    QN in the registry. ClientImpl.greet must therefore OVERRIDES
    Greeter.greet across files.
    """
    create_and_run_updater(
        kotlin_cross_file_project, mock_ingestor, skip_if_missing="kotlin"
    )

    overrides = get_relationships(mock_ingestor, "OVERRIDES")

    matching = [
        call
        for call in overrides
        if call.args[0][2].endswith("ClientImpl.greet")
        and call.args[2][2].endswith("Greeter.greet")
    ]
    assert matching, (
        "Expected ClientImpl.greet -[OVERRIDES]-> Greeter.greet across files; "
        f"all OVERRIDES edges: {[(c.args[0][2], c.args[2][2]) for c in overrides]}"
    )


def test_resolve_kotlin_parent_prefers_closer_package() -> None:
    """Regression: when multiple classes share the same simple name, the
    longest-common-prefix heuristic must prefer the candidate in the closest
    package to the child rather than picking alphabetically.

    Scenario: `project.pkg_b.Consumer` inherits `Result` without an explicit
    import (same-package, no import needed). At parse time the registry is
    empty so `_resolve_to_qn` falls back to `project.pkg_b.Consumer.Result`.
    The deferred pass then has two candidates:
      - `project.pkg_a.WrongResult.Result`  (different package — alphabetically first)
      - `project.pkg_b.CorrectResult.Result` (same package — higher prefix overlap)

    Without the prefix heuristic, sorted() picks the wrong candidate.
    """
    from codebase_rag.parsers.class_ingest.kotlin_inheritance import (
        _resolve_kotlin_parent,
    )

    parent_qn = "project.pkg_b.Consumer.Result"
    function_registry: dict = {
        "project.pkg_a.WrongResult.Result": NodeType.CLASS,
        "project.pkg_b.CorrectResult.Result": NodeType.CLASS,
    }
    simple_name_lookup: dict = {
        "Result": {
            "project.pkg_a.WrongResult.Result",
            "project.pkg_b.CorrectResult.Result",
        }
    }

    node_type, resolved_qn = _resolve_kotlin_parent(
        parent_qn, function_registry, simple_name_lookup
    )

    assert resolved_qn == "project.pkg_b.CorrectResult.Result", (
        f"Prefix heuristic should have preferred the same-package candidate; "
        f"got {resolved_qn!r}"
    )
    assert node_type == NodeType.CLASS


def test_resolve_kotlin_parent_prefers_interface_over_class() -> None:
    """Interface candidates take priority over Class candidates even when the
    Interface has a shorter prefix overlap.
    """
    from codebase_rag.parsers.class_ingest.kotlin_inheritance import (
        _resolve_kotlin_parent,
    )

    parent_qn = "project.pkg_a.Consumer.Callback"
    function_registry: dict = {
        "project.pkg_a.NearCallback.Callback": NodeType.CLASS,
        "project.pkg_b.FarCallback.Callback": NodeType.INTERFACE,
    }
    simple_name_lookup: dict = {
        "Callback": {
            "project.pkg_a.NearCallback.Callback",
            "project.pkg_b.FarCallback.Callback",
        }
    }

    node_type, resolved_qn = _resolve_kotlin_parent(
        parent_qn, function_registry, simple_name_lookup
    )

    assert node_type == NodeType.INTERFACE, (
        f"Interface candidate should win over Class regardless of prefix; got {node_type}"
    )
    assert resolved_qn == "project.pkg_b.FarCallback.Callback"
