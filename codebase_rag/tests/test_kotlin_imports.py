from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.kotlin import utils as kotlin_utils


@pytest.fixture
def kotlin_imports_source() -> bytes:
    return b"""
package x.y

import kotlin.collections.List
import kotlin.collections.*
import kotlin.text.Regex as R
"""


def test_kotlin_imports_unit(kotlin_imports_source: bytes) -> None:
    parsers, queries = load_parsers()
    if "kotlin" not in parsers:
        pytest.skip("kotlin parser not available")
    parser = queries["kotlin"]["parser"]
    tree = parser.parse(kotlin_imports_source)
    imports = kotlin_utils.extract_imports(tree.root_node)

    paths = {imp.path for imp in imports}
    assert "kotlin.collections.List" in paths, paths
    assert "kotlin.collections" in paths, paths
    assert "kotlin.text.Regex" in paths, paths

    by_path = {imp.path: imp for imp in imports}
    assert by_path["kotlin.collections"].is_wildcard is True
    assert by_path["kotlin.text.Regex"].alias == "R"
    assert by_path["kotlin.collections.List"].alias is None


@pytest.fixture
def kotlin_imports_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_imports"
    project_path.mkdir()
    (project_path / "App.kt").write_text(
        encoding="utf-8",
        data="""
package app

import kotlin.collections.List
import kotlin.collections.*
import kotlin.text.Regex as R

fun foo(): Int = 1
""",
    )
    return project_path


def test_kotlin_imports_in_processor(
    kotlin_imports_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    from codebase_rag.tests.conftest import create_and_run_updater

    updater = create_and_run_updater(
        kotlin_imports_project, mock_ingestor, skip_if_missing="kotlin"
    )

    project_name = kotlin_imports_project.name
    module_qn = f"{project_name}.App"
    mappings = updater.factory.import_processor.import_mapping.get(module_qn, {})

    assert mappings.get("List") == "kotlin.collections.List", mappings
    assert mappings.get("R") == "kotlin.text.Regex", mappings
    assert mappings.get("*kotlin.collections") == "kotlin.collections", mappings
