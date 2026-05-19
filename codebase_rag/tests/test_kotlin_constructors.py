from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_constructors_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_constructors"
    project_path.mkdir()
    (project_path / "Person.kt").write_text(
        encoding="utf-8",
        data="""
package people

class Person(val name: String) {
    var age: Int = 0
    constructor(name: String, age: Int) : this(name) {
        this.age = age
    }

    constructor() : this("anonymous", 0)
}
""",
    )
    return project_path


def test_kotlin_constructors_ingested(
    kotlin_constructors_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_constructors_project, mock_ingestor, skip_if_missing="kotlin"
    )

    methods = get_node_names(mock_ingestor, NodeType.METHOD)

    # Two secondary constructors + one primary should produce <init> entries
    init_entries = [name for name in methods if "<init>" in name]
    assert len(init_entries) >= 2, methods
