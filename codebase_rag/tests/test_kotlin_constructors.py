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

    # One primary + two secondary constructors must each ingest as <init>.
    # Asserting >= 3 (rather than >= 2) ensures a regression that drops the
    # primary fails the test — historically it could pass with the primary
    # silently missing because the function query was scoped to class_body
    # while primary_constructor lives at the class_declaration level.
    init_entries = [name for name in methods if "<init>" in name]
    assert len(init_entries) >= 3, methods


def test_kotlin_primary_constructor_ingested_with_no_body(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A class declared with only an inline primary constructor and no
    `{ ... }` body still ingests its constructor as a Method.
    """
    project_path = temp_repo / "kotlin_primary_only"
    project_path.mkdir()
    (project_path / "User.kt").write_text(
        encoding="utf-8",
        data="package u\n\nclass User(val name: String, val age: Int)\n",
    )

    create_and_run_updater(project_path, mock_ingestor, skip_if_missing="kotlin")

    methods = get_node_names(mock_ingestor, NodeType.METHOD)
    init_entries = [m for m in methods if "<init>" in m]
    assert init_entries, (
        f"Primary constructor of body-less class should ingest as <init>; "
        f"got methods={methods}"
    )
