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


@pytest.fixture
def kotlin_cross_file_import_project(temp_repo: Path) -> Path:
    """Two Kotlin files in different packages where one imports a class from
    the other. The Kotlin package index built lazily by ImportProcessor must
    bridge the package-form import (`a.b.Foo`) to the canonical
    file-path-rooted Module/Class QNs the ingestion pipeline registers.
    """
    project_path = temp_repo / "kotlin_xfile_imports"
    project_path.mkdir()
    (project_path / "Foo.kt").write_text(
        encoding="utf-8",
        data="""
package a.b

class Foo
""",
    )
    (project_path / "Bar.kt").write_text(
        encoding="utf-8",
        data="""
package c.d

import a.b.Foo

class Bar : Foo()
""",
    )
    return project_path


def test_kotlin_cross_file_import_resolves_to_internal_module(
    kotlin_cross_file_import_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: `import a.b.Foo` must resolve to the actual `Foo.kt`
    Module node, not to a synthetic external `Module(qualified_name='a.b')`.
    Without the lazy Kotlin package index, ImportProcessor falls through to
    `_ensure_external_module_node` and creates a fake external module.
    """
    from codebase_rag.tests.conftest import create_and_run_updater

    updater = create_and_run_updater(
        kotlin_cross_file_import_project, mock_ingestor, skip_if_missing="kotlin"
    )

    project_name = kotlin_cross_file_import_project.name
    bar_module_qn = f"{project_name}.Bar"
    foo_module_qn = f"{project_name}.Foo"
    foo_class_qn = f"{project_name}.Foo.Foo"

    # 1. import_mapping carries the canonical Class QN so call resolution
    #    can find Foo via the function_registry.
    mappings = updater.factory.import_processor.import_mapping.get(
        bar_module_qn, {}
    )
    assert mappings.get("Foo") == foo_class_qn, (
        f"Expected import_mapping['{bar_module_qn}']['Foo'] == "
        f"'{foo_class_qn}'; got {mappings}"
    )

    # 2. The IMPORTS edge from Bar's Module points to the actual Foo.kt
    #    Module node, not to a synthetic external `a.b`.
    imports_edges = [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(c.args) >= 3 and c.args[1] == "IMPORTS"
    ]
    bar_imports_targets = {
        c.args[2][2]
        for c in imports_edges
        if c.args[0][2] == bar_module_qn
    }
    assert foo_module_qn in bar_imports_targets, (
        f"Expected IMPORTS edge from {bar_module_qn} to {foo_module_qn}; "
        f"got targets {bar_imports_targets}"
    )

    # 3. No synthetic external Module(qualified_name='a.b') was created.
    external_modules = [
        c
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if c.args[0] == "Module" and c.args[1].get("is_external") is True
    ]
    bad_external = [
        c.args[1] for c in external_modules
        if c.args[1].get("qualified_name") == "a.b"
    ]
    assert not bad_external, (
        f"Internal Kotlin package 'a.b' must not be ingested as an external "
        f"Module; got {bad_external}"
    )


@pytest.fixture
def kotlin_kts_import_project(temp_repo: Path) -> Path:
    """A `.kts` script declares a class that a `.kt` file imports. The Kotlin
    package index must scan both extensions; otherwise the `.kts`
    declaration is invisible and the import resolves to a synthetic
    external Module.
    """
    project_path = temp_repo / "kotlin_kts_imports"
    project_path.mkdir()
    (project_path / "Build.kts").write_text(
        encoding="utf-8",
        data="""
package a.b

class Foo
""",
    )
    (project_path / "App.kt").write_text(
        encoding="utf-8",
        data="""
package c.d

import a.b.Foo

class App : Foo()
""",
    )
    return project_path


def test_kotlin_kts_file_indexed(
    kotlin_kts_import_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Regression: the Kotlin package index must walk `.kts` script files,
    not only `.kt`. parser_loader advertises both extensions; the bridge
    has to keep up.
    """
    from codebase_rag.tests.conftest import create_and_run_updater

    updater = create_and_run_updater(
        kotlin_kts_import_project, mock_ingestor, skip_if_missing="kotlin"
    )

    project_name = kotlin_kts_import_project.name
    app_module_qn = f"{project_name}.App"
    build_module_qn = f"{project_name}.Build"
    foo_class_qn = f"{project_name}.Build.Foo"

    mappings = updater.factory.import_processor.import_mapping.get(
        app_module_qn, {}
    )
    assert mappings.get("Foo") == foo_class_qn, (
        f"Expected import_mapping['{app_module_qn}']['Foo'] == "
        f"'{foo_class_qn}'; got {mappings}"
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
    assert build_module_qn in app_imports_targets, (
        f"Expected IMPORTS edge from {app_module_qn} to {build_module_qn}; "
        f"got targets {app_imports_targets}"
    )
