from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_enums_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_enums"
    project_path.mkdir()
    (project_path / "Enums.kt").write_text(
        encoding="utf-8",
        data="""
package enums

enum class Color { RED, GREEN, BLUE }

enum class Status(val code: Int) {
    ACTIVE(1),
    INACTIVE(0);

    fun describe(): String = "status=$code"
}

class Plain(val name: String)

data class User(val id: Int, val name: String)
""",
    )
    return project_path


def test_kotlin_enum_classes_become_enum_nodes(
    kotlin_enums_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_enums_project, mock_ingestor, skip_if_missing="kotlin"
    )

    enums = get_node_names(mock_ingestor, NodeType.ENUM)
    assert any(name.endswith("Color") for name in enums), enums
    assert any(name.endswith("Status") for name in enums), enums


def test_kotlin_non_enum_classes_remain_class_nodes(
    kotlin_enums_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_enums_project, mock_ingestor, skip_if_missing="kotlin"
    )

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    assert any(name.endswith("Plain") for name in classes), classes
    assert any(name.endswith("User") for name in classes), classes

    # Enums must NOT be mis-classified as Class nodes.
    assert not any(name.endswith("Color") for name in classes), classes
    assert not any(name.endswith("Status") for name in classes), classes
