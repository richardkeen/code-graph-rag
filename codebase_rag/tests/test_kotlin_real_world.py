from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
)
from codebase_rag.types_defs import NodeType

KOTLIN_FIXTURE = """
package app

import kotlin.collections.*
import kotlin.text.Regex as R

@Service
data class User(val id: Int, val name: String) {
    companion object {
        const val DEFAULT_NAME = "anon"
        fun create(id: Int): User = User(id, DEFAULT_NAME)
    }
}

sealed class Result {
    data class Ok(val value: Int) : Result()
    data class Err(val msg: String) : Result()
}

@JvmInline
value class Email(val v: String) {
    fun normalized(): String = v.lowercase()
}

object Registry {
    private val users: MutableList<User> = mutableListOf()

    fun register(user: User): Result {
        users.add(user)
        return Result.Ok(users.size)
    }
}

fun String.shout(): String = this.uppercase()

fun main() {
    val u = User.create(1)
    val out = Registry.register(u)
    when (out) {
        is Result.Ok -> println("ok: ${out.value}")
        is Result.Err -> println("err: ${out.msg}")
    }
    println("HELLO".shout())
    val pair = 1 to 2
}
"""


@pytest.fixture
def kotlin_real_world_project(temp_repo: Path) -> Path:
    project_path = temp_repo / "kotlin_real_world"
    project_path.mkdir()
    (project_path / "App.kt").write_text(KOTLIN_FIXTURE, encoding="utf-8")
    return project_path


def test_kotlin_real_world_no_parser_warnings(
    kotlin_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Ingest a moderately complex Kotlin fixture and assert minimal warning count.

    Mirrors the warning-counter pattern from test_java_real_world.py.
    """
    warning_messages: list[str] = []

    sink_id = logger.add(
        lambda message: warning_messages.append(str(message)),
        level="WARNING",
    )
    try:
        create_and_run_updater(
            kotlin_real_world_project, mock_ingestor, skip_if_missing="kotlin"
        )
    finally:
        logger.remove(sink_id)

    # Allow a small noise budget for grammar weaknesses (when exhaustiveness etc.).
    kotlin_warnings = [
        msg for msg in warning_messages if "kotlin" in msg.lower() or "ERROR" in msg
    ]
    assert len(kotlin_warnings) <= 5, kotlin_warnings


def test_kotlin_real_world_node_counts(
    kotlin_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    create_and_run_updater(
        kotlin_real_world_project, mock_ingestor, skip_if_missing="kotlin"
    )

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    functions = get_node_names(mock_ingestor, NodeType.FUNCTION)
    methods = get_node_names(mock_ingestor, NodeType.METHOD)

    # User, Companion, Result, Ok, Err, Email, Registry — at minimum
    assert len(classes) >= 5, classes
    # main + String.shout + at least the body-bound module-level fns
    assert len(functions) >= 2, functions
    # create, normalized, register at minimum
    assert len(methods) >= 3, methods
