from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.kotlin import utils as kotlin_utils
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_extensions_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_extensions"
    project_path.mkdir()
    (project_path / "Strings.kt").write_text(
        encoding="utf-8",
        data="""
package strings

fun String.shout(): String = this.uppercase()
fun List<Int>.sumPlusOne(): Int = this.sum() + 1
""",
    )
    return project_path


def test_kotlin_extension_function_qn_includes_receiver(
    kotlin_extensions_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_extensions_project, mock_ingestor, skip_if_missing="kotlin"
    )

    functions = get_node_names(mock_ingestor, NodeType.FUNCTION)

    assert any(name.endswith(".String.shout") for name in functions), functions
    assert any(name.endswith(".List.sumPlusOne") for name in functions), functions


def test_kotlin_receiver_type_extraction_unit() -> None:
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    if "kotlin" not in parsers:
        pytest.skip("kotlin parser not available")
    parser = queries["kotlin"]["parser"]
    src = b"fun String.shout(): String = this"
    tree = parser.parse(src)
    fn_node = next(
        c for c in tree.root_node.children if c.type == "function_declaration"
    )
    info = kotlin_utils.extract_function_info(fn_node)
    assert info.name == "shout"
    assert info.receiver_type == "String"
