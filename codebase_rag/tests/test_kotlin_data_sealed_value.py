from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.kotlin import utils as kotlin_utils
from codebase_rag.parsers.utils import safe_decode_text
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType


@pytest.fixture
def kotlin_modifiers_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_modifiers"
    project_path.mkdir()
    (project_path / "Models.kt").write_text(
        encoding="utf-8",
        data="""
package models

data class User(val id: Int, val name: String)

sealed class Result {
    class Ok(val value: Int) : Result()
    class Err(val message: String) : Result()
}

@JvmInline
value class Email(val v: String)
""",
    )
    return project_path


def test_kotlin_modifier_classes_ingested(
    kotlin_modifiers_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_modifiers_project, mock_ingestor, skip_if_missing="kotlin"
    )

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    assert any(name.endswith("User") for name in classes), classes
    assert any(name.endswith("Result") for name in classes), classes
    assert any(name.endswith("Email") for name in classes), classes
    assert any(name.endswith("Ok") for name in classes), classes
    assert any(name.endswith("Err") for name in classes), classes


def test_kotlin_modifier_extraction_unit() -> None:
    """Direct unit test on the modifier extractor against a parsed AST."""
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    if "kotlin" not in parsers:
        pytest.skip("kotlin parser not available")
    parser = queries["kotlin"]["parser"]
    src = b"""
data class User(val id: Int)
sealed class Result
@JvmInline
value class Email(val v: String)
""".strip()
    tree = parser.parse(src)
    modifiers_seen: list[list[str]] = []
    annotations_seen: list[list[str]] = []
    for child in tree.root_node.children:
        if child.type == "class_declaration":
            modifiers_seen.append(kotlin_utils.extract_class_modifiers(child))
            annotations_seen.append(kotlin_utils.extract_annotations(child))

    flat_modifiers = {m for ms in modifiers_seen for m in ms}
    assert "data" in flat_modifiers, flat_modifiers
    assert "sealed" in flat_modifiers, flat_modifiers
    assert "value" in flat_modifiers, flat_modifiers

    flat_annotations = {a for ans in annotations_seen for a in ans}
    assert "JvmInline" in flat_annotations, flat_annotations

    # Sanity that we read the text correctly.
    for child in tree.root_node.children:
        if child.type == "class_declaration":
            assert safe_decode_text(child) is not None
