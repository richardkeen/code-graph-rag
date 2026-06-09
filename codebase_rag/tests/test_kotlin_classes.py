from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
    get_relationships,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_classes_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_classes"
    project_path.mkdir()
    (project_path / "Shapes.kt").write_text(
        encoding="utf-8",
        data="""
package shapes

open class Shape(val name: String) {
    open fun area(): Double = 0.0
    fun describe(): String = "shape: $name"
}

abstract class Polygon(name: String) : Shape(name) {
    abstract fun sides(): Int
}

class Square(side: Int) : Polygon("square") {
    override fun sides(): Int = 4
    override fun area(): Double = (4 * 4).toDouble()
}
""",
    )
    return project_path


def test_kotlin_classes_and_methods_inside(
    kotlin_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_classes_project, mock_ingestor, skip_if_missing="kotlin"
    )

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    methods = get_node_names(mock_ingestor, NodeType.METHOD)

    assert any(name.endswith("Shape") for name in classes), classes
    assert any(name.endswith("Polygon") for name in classes), classes
    assert any(name.endswith("Square") for name in classes), classes

    # B1 hook: methods inside classes must be ingested
    assert any(name.endswith(".area") or ".area(" in name for name in methods), methods
    assert any(
        name.endswith(".describe") or ".describe(" in name for name in methods
    ), methods
    assert any(name.endswith(".sides") or ".sides(" in name for name in methods), (
        methods
    )


def test_kotlin_class_inheritance_edge(
    kotlin_classes_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_classes_project, mock_ingestor, skip_if_missing="kotlin"
    )

    inherits = get_relationships(mock_ingestor, "INHERITS")

    targets = {(c.args[0][2], c.args[2][2]) for c in inherits}
    assert any(
        src.endswith("Square") and tgt.endswith("Polygon") for src, tgt in targets
    ), targets


def test_kotlin_typealias_ingested_by_alias_name(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression guard: typealias query captures LHS alias name (not RHS type).

    tree-sitter-kotlin's type_alias grammar exposes the alias identifier via
    the `type:` field, which is the LHS name. The class_query captures it as
    @name.  This test asserts both a simple alias and a parameterised-RHS alias
    are ingested under the correct alias name, not the RHS type.
    """
    project = temp_repo / "kotlin_typealias"
    project.mkdir()
    (project / "Aliases.kt").write_text(
        encoding="utf-8",
        data="""
package aliases

typealias UserId = String
typealias UserMap = Map<String, String>
""",
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="kotlin")

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    assert any(name.endswith("UserId") for name in classes), (
        f"UserId typealias should be ingested as a Class; got {classes}"
    )
    assert any(name.endswith("UserMap") for name in classes), (
        f"UserMap typealias should be ingested as a Class; got {classes}"
    )
    # Must NOT capture the RHS identifier instead of the alias name.
    assert not any(name.endswith(".String") for name in classes), (
        f"RHS 'String' should not be ingested as an alias node; got {classes}"
    )


@pytest.fixture
def kotlin_qualified_supertype_project(temp_repo: Path) -> Path:
    """Two Kotlin files in different packages where the child class names its
    parent with a fully qualified type (`foo.bar.Base`) instead of relying on
    an `import`. tree-sitter-kotlin emits the parent as a `user_type` node
    holding multiple `identifier` children; the supertype walker must
    reconstruct the full dotted name so `_resolve_kotlin_parent`'s simple-name
    fallback can still locate Base in the registry.
    """
    project_path = temp_repo / "kotlin_qualified_supertype"
    project_path.mkdir()
    (project_path / "Base.kt").write_text(
        encoding="utf-8",
        data="""
package foo.bar

open class Base
""",
    )
    (project_path / "App.kt").write_text(
        encoding="utf-8",
        data="""
package c.d

class C : foo.bar.Base()
""",
    )
    return project_path


def test_kotlin_qualified_supertype_resolves(
    kotlin_qualified_supertype_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: `class C : foo.bar.Base()` must produce an INHERITS edge to
    the canonical Base Class QN, not to a truncated `foo` (the prior bug
    where _kotlin_user_type_name returned only the first identifier child).
    """
    create_and_run_updater(
        kotlin_qualified_supertype_project,
        mock_ingestor,
        skip_if_missing="kotlin",
    )

    project_name = kotlin_qualified_supertype_project.name
    base_class_qn = f"{project_name}.Base.Base"
    child_class_qn = f"{project_name}.App.C"

    inherits_edges = get_relationships(mock_ingestor, "INHERITS")
    inherits_targets_for_c = {
        edge.args[2][2]
        for edge in inherits_edges
        if edge.args[0][2] == child_class_qn
    }

    assert base_class_qn in inherits_targets_for_c, (
        f"Expected INHERITS edge from {child_class_qn} to {base_class_qn}; "
        f"got {inherits_targets_for_c}"
    )
    assert not any(
        target.endswith(".foo") or target == "foo"
        for target in inherits_targets_for_c
    ), (
        f"INHERITS target must not be the truncated first identifier 'foo'; "
        f"got {inherits_targets_for_c}"
    )
