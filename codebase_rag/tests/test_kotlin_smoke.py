from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_smoke_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_smoke"
    project_path.mkdir()
    (project_path / "Hello.kt").write_text(
        encoding="utf-8",
        data="""
package com.example

class Greeter(val name: String) {
    fun greet(): String {
        return "Hello, $name"
    }
}

fun main() {
    val g = Greeter("world")
    println(g.greet())
}
""",
    )
    return project_path


def test_kotlin_smoke_ingest(
    kotlin_smoke_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Phase A acceptance: ≥1 Module + ≥1 Class + ≥1 Function ingested with names."""
    create_and_run_updater(
        kotlin_smoke_project, mock_ingestor, skip_if_missing="kotlin"
    )

    modules = get_node_names(mock_ingestor, NodeType.MODULE)
    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    functions = get_node_names(mock_ingestor, NodeType.FUNCTION)

    assert any("Hello" in m for m in modules), f"expected Hello module in {modules!r}"
    assert any(name.endswith("Greeter") for name in classes), (
        f"expected Greeter class in {classes!r}"
    )
    assert any(name.endswith(".main") for name in functions), (
        f"expected top-level main fn in {functions!r}"
    )
