from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import QueryCursor

from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_relationships,
)


@pytest.fixture
def kotlin_calls_source() -> bytes:
    return b"""
package app

fun main() {
    val xs = listOf(1, 2, 3).map { it * 2 }.filter { it > 2 }
    val pair = 1 to 2
    println(xs.size)
    val u = User.create(1)
}
"""


def test_kotlin_call_query_captures_only_calls(kotlin_calls_source: bytes) -> None:
    """Direct test on the call query: each capture should correspond to a real call."""
    parsers, queries = load_parsers()
    if "kotlin" not in parsers:
        pytest.skip("kotlin parser not available")
    parser = queries["kotlin"]["parser"]
    call_query = queries["kotlin"]["calls"]
    tree = parser.parse(kotlin_calls_source)

    cursor = QueryCursor(call_query)
    captures = cursor.captures(tree.root_node)

    call_nodes = captures.get("call", [])
    names = captures.get("name", [])

    name_texts = {n.text.decode() for n in names if n.text}
    # Should include: listOf, map, filter, to, println, create
    expected = {"listOf", "map", "filter", "to", "println", "create"}
    assert expected.issubset(name_texts), name_texts

    # Property accesses (e.g. xs.size, u.name) must NOT show up as calls
    assert "size" not in name_texts, name_texts
    # And no spurious `xs` from the property declaration target
    assert all(n.type in ("call_expression", "infix_expression") for n in call_nodes), {
        n.type for n in call_nodes
    }


@pytest.fixture
def kotlin_calls_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_calls"
    project_path.mkdir()
    (project_path / "App.kt").write_text(
        encoding="utf-8",
        data="""
package app

class User(val id: Int) {
    companion object {
        fun create(id: Int): User = User(id)
    }
}

fun main() {
    val u = User.create(1)
    println(u.id)
    val xs = listOf(1,2,3).map { it * 2 }
    val p = 1 to 2
}
""",
    )
    return project_path


def test_kotlin_calls_ingest_does_not_crash(
    kotlin_calls_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Ingestion must run cleanly on a Kotlin file with chained, infix, and companion calls.

    CALLS resolution into graph edges depends on the call_processor recognizing the
    Kotlin call shape. The call_query captures are validated separately; this test
    simply ensures the structured queries don't break the pipeline on real Kotlin.
    """
    create_and_run_updater(
        kotlin_calls_project, mock_ingestor, skip_if_missing="kotlin"
    )
    # Ingestion completed without raising.
    _ = get_relationships(mock_ingestor, "CALLS")
