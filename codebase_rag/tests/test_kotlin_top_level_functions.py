from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_top_level_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_top_level"
    project_path.mkdir()
    (project_path / "Math.kt").write_text(
        encoding="utf-8",
        data="""
package x.y

fun add(a: Int, b: Int): Int = a + b
fun greet(name: String): String = "Hello, " + name
fun noArgs() = 42
""",
    )
    return project_path


def test_kotlin_top_level_functions(
    kotlin_top_level_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_top_level_project, mock_ingestor, skip_if_missing="kotlin"
    )

    functions = get_node_names(mock_ingestor, NodeType.FUNCTION)

    assert any(name.endswith(".add") for name in functions), functions
    assert any(name.endswith(".greet") for name in functions), functions
    assert any(name.endswith(".noArgs") for name in functions), functions
