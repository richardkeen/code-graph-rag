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


@pytest.fixture
def kotlin_cross_file_extension_import_project(temp_repo: Path) -> Path:
    """One file declares a top-level extension; another imports and calls it.

    The Kotlin package index must record the receiver-prefixed canonical QN
    (`<module>.<receiver>.<name>`), not just the simple name; otherwise
    `import_mapping` points at a QN that never exists in `function_registry`,
    and the IMPORTS edge targets the wrong Module.
    """
    project_path = temp_repo / "kotlin_xfile_extensions"
    project_path.mkdir()
    (project_path / "Strings.kt").write_text(
        encoding="utf-8",
        data="""
package strings

fun String.shout(): String = this.uppercase()
""",
    )
    (project_path / "App.kt").write_text(
        encoding="utf-8",
        data="""
package app

import strings.shout

fun main() {
    "hi".shout()
}
""",
    )
    return project_path


def test_kotlin_cross_file_extension_function_import_resolves(
    kotlin_cross_file_extension_import_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: importing a top-level extension `fun String.shout()` from
    another file must resolve to the canonical receiver-prefixed QN
    (`<project>.Strings.String.shout`) and an IMPORTS edge targeting the
    Module (`<project>.Strings`), not the synthetic `<project>.Strings.shout`.
    """
    updater = create_and_run_updater(
        kotlin_cross_file_extension_import_project,
        mock_ingestor,
        skip_if_missing="kotlin",
    )

    project_name = kotlin_cross_file_extension_import_project.name
    app_module_qn = f"{project_name}.App"
    strings_module_qn = f"{project_name}.Strings"
    shout_canonical_qn = f"{project_name}.Strings.String.shout"

    mappings = updater.factory.import_processor.import_mapping.get(
        app_module_qn, {}
    )
    assert mappings.get("shout") == shout_canonical_qn, (
        f"Expected import_mapping['{app_module_qn}']['shout'] == "
        f"'{shout_canonical_qn}'; got {mappings}"
    )

    imports_edges = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) >= 3 and c.args[1] == "IMPORTS"
    ]
    app_imports_targets = {
        c.args[2][2]
        for c in imports_edges
        if c.args[0][2] == app_module_qn
    }
    assert strings_module_qn in app_imports_targets, (
        f"Expected IMPORTS edge from {app_module_qn} to {strings_module_qn}; "
        f"got targets {app_imports_targets}"
    )
    assert f"{strings_module_qn}.String" not in app_imports_targets, (
        f"IMPORTS edge must target the Module, not the receiver-qualified "
        f"prefix; got {app_imports_targets}"
    )


@pytest.fixture
def kotlin_wildcard_extension_project(temp_repo: Path) -> Path:
    """One file declares a top-level extension; another wildcard-imports the
    package and calls the extension. The wildcard call resolver only probes
    `<imported_qn>.<call_name>`, so the package index has to surface
    `<module>.<receiver>` prefixes for extension functions or `import
    strings.*; "x".shout()` silently drops the CALLS edge.
    """
    project_path = temp_repo / "kotlin_wildcard_ext"
    project_path.mkdir()
    (project_path / "Strings.kt").write_text(
        encoding="utf-8",
        data="""
package strings

fun String.shout(): String = this.uppercase()
""",
    )
    (project_path / "App.kt").write_text(
        encoding="utf-8",
        data="""
package app

import strings.*

fun main() {
    "hi".shout()
}
""",
    )
    return project_path


def test_kotlin_wildcard_extension_resolves(
    kotlin_wildcard_extension_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: `import strings.*` followed by `"hi".shout()` must
    resolve to the canonical extension QN `<project>.Strings.String.shout`
    via the wildcard probe. Requires the package index to expose the
    extension's `<module>.<receiver>` prefix per package so the wildcard
    expansion in `_parse_kotlin_imports` can store an `import_mapping`
    entry the resolver lands on.
    """
    updater = create_and_run_updater(
        kotlin_wildcard_extension_project,
        mock_ingestor,
        skip_if_missing="kotlin",
    )

    project_name = kotlin_wildcard_extension_project.name
    app_module_qn = f"{project_name}.App"
    receiver_prefix = f"{project_name}.Strings.String"
    shout_canonical_qn = f"{project_name}.Strings.String.shout"

    mappings = updater.factory.import_processor.import_mapping.get(
        app_module_qn, {}
    )
    assert mappings.get(f"*strings@{receiver_prefix}") == receiver_prefix, (
        f"Expected wildcard prefix entry "
        f"'*strings@{receiver_prefix}' -> '{receiver_prefix}'; got {mappings}"
    )

    calls_edges = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) >= 3 and c.args[1] == "CALLS"
    ]
    main_call_targets = {
        c.args[2][2]
        for c in calls_edges
        if isinstance(c.args[0][2], str) and c.args[0][2].endswith(".main")
    }
    assert shout_canonical_qn in main_call_targets, (
        f"Expected CALLS edge from main() to {shout_canonical_qn} via the "
        f"wildcard import; got {main_call_targets}"
    )
