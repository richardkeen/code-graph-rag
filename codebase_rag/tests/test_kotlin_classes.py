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
