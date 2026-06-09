"""Build a `<package>.<simple_name>` → canonical-QN index for Kotlin.

Kotlin imports are written in package form (`import a.b.Foo`) but Kotlin
Modules and Classes are ingested with file-path-rooted qualified names
(`<project>.<relative_file_path>.Foo`). Without a bridge between the two
namespaces, `import a.b.Foo` flows through the external-module path in
`ImportProcessor._resolve_module_path` and produces a synthetic `Module`
node — even though `Foo.kt` already exists in the graph.

This helper walks every `.kt` file in the repo, parses each with
tree-sitter-kotlin, reads its `package` declaration, and pulls each
top-level declaration's name. The resulting map lets `_resolve_module_path`
turn `a.b.Foo` into the actual ingested `<project>.<file>.Foo` qualified
name.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from tree_sitter import Language, Parser

from ... import constants as cs
from ...utils.path_utils import should_skip_path
from . import utils as kotlin_utils


def build_kotlin_package_index(
    repo_path: Path, project_name: str
) -> dict[str, str]:
    parser = _make_kotlin_parser()
    if parser is None:
        return {}
    index: dict[str, str] = {}
    for ext in cs.KOTLIN_EXTENSIONS:
        for file_path in repo_path.rglob(f"*{ext}"):
            if should_skip_path(file_path, repo_path):
                continue
            try:
                tree = parser.parse(file_path.read_bytes())
            except Exception:
                continue
            package = kotlin_utils.extract_package_name(tree.root_node)
            if package is None:
                continue
            module_qn = _file_to_module_qn(file_path, repo_path, project_name)
            if module_qn is None:
                continue
            for name in _iter_top_level_named_decls(tree.root_node):
                index[f"{package}{cs.SEPARATOR_DOT}{name}"] = (
                    f"{module_qn}{cs.SEPARATOR_DOT}{name}"
                )
    return index


def _make_kotlin_parser() -> Parser | None:
    try:
        module = importlib.import_module(cs.TreeSitterModule.KOTLIN.value)
    except ImportError:
        return None
    try:
        language = Language(module.language())
    except Exception:
        return None
    return Parser(language)


_TOP_LEVEL_NAMED_DECL_TYPES = frozenset(
    {
        cs.TS_KOTLIN_CLASS_DECLARATION,
        cs.TS_KOTLIN_OBJECT_DECLARATION,
        cs.TS_KOTLIN_FUNCTION_DECLARATION,
        cs.TS_KOTLIN_TYPE_ALIAS,
    }
)


def _iter_top_level_named_decls(root_node) -> list[str]:
    names: list[str] = []
    for child in root_node.children:
        if child.type not in _TOP_LEVEL_NAMED_DECL_TYPES:
            continue
        # type_alias uses the `type:` field for its declared name; everything
        # else uses `name:`.
        field = (
            cs.TS_FIELD_TYPE
            if child.type == cs.TS_KOTLIN_TYPE_ALIAS
            else cs.TS_FIELD_NAME
        )
        name_node = child.child_by_field_name(field)
        if name_node is None or name_node.text is None:
            continue
        names.append(name_node.text.decode(cs.ENCODING_UTF8))
    return names


def _file_to_module_qn(
    file_path: Path, repo_path: Path, project_name: str
) -> str | None:
    try:
        relative = file_path.relative_to(repo_path)
    except ValueError:
        return None
    parts = list(relative.with_suffix("").parts)
    if not parts:
        return None
    return cs.SEPARATOR_DOT.join([project_name, *parts])
