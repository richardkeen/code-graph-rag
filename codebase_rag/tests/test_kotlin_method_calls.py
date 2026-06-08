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


@pytest.fixture
def kotlin_method_calls_project(temp_repo: Path) -> Path:
    """Calls inside a class method. tree-sitter-kotlin class nodes don't
    expose a `body` field, so call_processor must use the handler's
    find_class_body hook to walk methods."""
    project_path = temp_repo / "kotlin_method_calls"
    project_path.mkdir()
    (project_path / "App.kt").write_text(
        encoding="utf-8",
        data="""
package app

class Service {
    fun run() {
        helper()
        println("done")
    }
}

fun helper() {}
""",
    )
    return project_path


def test_kotlin_class_method_calls_emit_edges(
    kotlin_method_calls_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: previously _process_calls_in_classes used
    `class_node.child_by_field_name("body")` which returns None for Kotlin,
    so methods inside Kotlin classes never produced CALLS edges. Routing
    through handler.find_class_body fixes this."""
    create_and_run_updater(
        kotlin_method_calls_project, mock_ingestor, skip_if_missing="kotlin"
    )

    calls = get_relationships(mock_ingestor, "CALLS")
    # Source should be the run() method's qualified name; we just need at
    # least one CALLS edge originating from inside the Service.run method.
    method_call_sources = {
        call.args[0][2]
        for call in calls
        if ".Service.run" in call.args[0][2]
    }
    assert method_call_sources, (
        f"Expected CALLS edges originating from Service.run, got "
        f"{[call.args[0][2] for call in calls]}"
    )


@pytest.fixture
def kotlin_parameterized_calls_project(temp_repo: Path) -> Path:
    """A class with parameterised methods that call each other. The caller
    QN on the CALLS edge must include the parameter signature, otherwise
    Memgraph's MATCH on the source endpoint cannot find the ingested
    Method node and the edge is silently dropped.
    """
    project_path = temp_repo / "kotlin_param_calls"
    project_path.mkdir()
    (project_path / "Calc.kt").write_text(
        encoding="utf-8",
        data="""
package app

class Calc {
    fun add(x: Int, y: Int): Int {
        return helper(x)
    }

    fun helper(n: Int): Int {
        return n + 1
    }
}
""",
    )
    return project_path


def test_kotlin_parameterized_caller_qn_includes_signature(
    kotlin_parameterized_calls_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: caller QN for Kotlin methods must carry the parameter
    signature so it matches the parameter-signature-qualified Method node
    QN that ingestion produced. Without this, CALLS edges from
    parameterised Kotlin methods are silently dropped at MATCH time.
    """
    create_and_run_updater(
        kotlin_parameterized_calls_project,
        mock_ingestor,
        skip_if_missing="kotlin",
    )

    calls = get_relationships(mock_ingestor, "CALLS")
    add_sources = {
        call.args[0][2] for call in calls if ".Calc.add" in call.args[0][2]
    }
    assert any("(" in source and ")" in source for source in add_sources), (
        f"Expected at least one CALLS source qn for Calc.add to include a "
        f"parameter signature, got {add_sources}"
    )


@pytest.fixture
def kotlin_companion_calls_project(temp_repo: Path) -> Path:
    """Companion-object methods are siblings of the outer class's body
    walker scope (KotlinHandler.find_class_body returns class_node).
    Without is_direct_class_member filtering, a call inside a companion
    method gets attributed to the outer class qualified name.
    """
    project_path = temp_repo / "kotlin_companion_calls"
    project_path.mkdir()
    (project_path / "Outer.kt").write_text(
        encoding="utf-8",
        data="""
package app

class Outer {
    fun outerFn() {
        println("outer")
    }

    companion object {
        fun makeOne(): Outer {
            return Outer()
        }
    }
}
""",
    )
    return project_path


def test_kotlin_companion_calls_attributed_to_companion_not_outer(
    kotlin_companion_calls_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: with KotlinHandler.find_class_body returning class_node,
    the function query running over the outer Class also captures methods
    defined inside the companion. Without the is_direct_class_member
    filter on call_processor, those captures get attributed to the outer
    class qualified name and produce CALLS edges from `Outer.makeOne`
    rather than from `Outer.Companion.makeOne`.
    """
    create_and_run_updater(
        kotlin_companion_calls_project,
        mock_ingestor,
        skip_if_missing="kotlin",
    )

    calls = get_relationships(mock_ingestor, "CALLS")
    sources = {call.args[0][2] for call in calls}

    # No CALLS edge should originate from `Outer.makeOne` directly — that
    # would mean the companion's makeOne was wrongly attributed to Outer.
    bad_sources = {s for s in sources if s.endswith(".Outer.makeOne")}
    assert not bad_sources, (
        f"Companion method makeOne should not produce CALLS edges from "
        f"Outer.makeOne (it belongs to the Companion class). Bad sources: "
        f"{bad_sources}; all sources: {sources}"
    )


@pytest.fixture
def kotlin_local_function_project(temp_repo: Path) -> Path:
    """A class method that declares a local function inside its body. Local
    functions must not be ingested as class methods.
    """
    project_path = temp_repo / "kotlin_local_fn"
    project_path.mkdir()
    (project_path / "A.kt").write_text(
        encoding="utf-8",
        data="""
package app

class A {
    fun outer() {
        fun inner() {}
        inner()
    }
}
""",
    )
    return project_path


def test_kotlin_local_function_not_classified_as_method(
    kotlin_local_function_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: KotlinHandler.is_direct_class_member must reject captures
    that have a function-like ancestor between them and the enclosing class.
    Otherwise, the unanchored function query — running over the whole class
    node — picks up local declarations inside method bodies and ingests them
    as class methods (e.g. `A.inner` for `class A { fun outer() { fun
    inner() {} } }`).
    """
    from codebase_rag.tests.conftest import get_node_names
    from codebase_rag.types_defs import NodeType

    create_and_run_updater(
        kotlin_local_function_project,
        mock_ingestor,
        skip_if_missing="kotlin",
    )

    methods = get_node_names(mock_ingestor, NodeType.METHOD)
    inner_methods = {m for m in methods if m.endswith(".inner")}
    assert not inner_methods, (
        f"Local function `inner` should NOT ingest as a Method. "
        f"Got methods ending in .inner: {inner_methods}; all methods: {methods}"
    )
