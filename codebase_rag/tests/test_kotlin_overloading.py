from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_overloading_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_overloading"
    project_path.mkdir()
    (project_path / "Calc.kt").write_text(
        encoding="utf-8",
        data="""
package calc

class Calc {
    fun fn(x: Int): Int = x
    fun fn(x: String): String = x
    fun fn(x: Int, y: Int): Int = x + y
}
""",
    )
    return project_path


def test_kotlin_overloads_disambiguated_by_param_types(
    kotlin_overloading_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_overloading_project, mock_ingestor, skip_if_missing="kotlin"
    )

    methods = get_node_names(mock_ingestor, NodeType.METHOD)

    fn_qns = [name for name in methods if ".fn" in name]
    assert any(name.endswith(".fn(Int)") for name in fn_qns), fn_qns
    assert any(name.endswith(".fn(String)") for name in fn_qns), fn_qns
    assert any(name.endswith(".fn(Int, Int)") for name in fn_qns), fn_qns
