from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_companions_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_companions"
    project_path.mkdir()
    (project_path / "Foo.kt").write_text(
        encoding="utf-8",
        data="""
package companions

class Foo(val x: Int) {
    companion object {
        fun bar(): Int = 1
    }
}

object Singleton {
    fun baz(): Int = 2
}
""",
    )
    return project_path


def test_kotlin_companion_member_qn(
    kotlin_companions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_companions_project, mock_ingestor, skip_if_missing="kotlin"
    )

    methods = get_node_names(mock_ingestor, NodeType.METHOD)

    # Companion: should look like ...Foo.Companion.bar(...)
    assert any(".Foo.Companion.bar" in name for name in methods), methods


def test_kotlin_object_member_qn(
    kotlin_companions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_companions_project, mock_ingestor, skip_if_missing="kotlin"
    )

    methods = get_node_names(mock_ingestor, NodeType.METHOD)
    assert any(".Singleton.baz" in name for name in methods), methods


def test_kotlin_companion_member_no_double_qualification(
    kotlin_companions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: a method on a companion must appear as ...Foo.Companion.bar exactly once.

    Both `_ingest_class_methods` paths (walking the outer class body, and walking the
    companion as its own class) used to ingest the same method, with the companion
    pass producing a doubled `Foo.Companion.Companion.bar` FQN. After the fix, only
    the companion's own pass runs and the FQN is single.
    """
    create_and_run_updater(
        kotlin_companions_project, mock_ingestor, skip_if_missing="kotlin"
    )

    methods = get_node_names(mock_ingestor, NodeType.METHOD)
    bar_methods = [name for name in methods if name.endswith("bar")]

    # No doubled qualifier
    assert not any(".Companion.Companion." in name for name in bar_methods), bar_methods
    # The single, correct FQN is present
    assert any(name.endswith(".Foo.Companion.bar") for name in bar_methods), bar_methods
    # And only one method node was emitted for `bar`
    assert len(bar_methods) == 1, bar_methods
