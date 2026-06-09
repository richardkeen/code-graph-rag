"""Build a Kotlin name → (canonical_qn, module_qn) index.

Kotlin imports are written in package form (`import a.b.Foo`) but
Modules / Classes / top-level functions are ingested with file-path-rooted
qualified names (`<project>.<relative_file_path>.<symbol>`). Without a
bridge between the two namespaces, `import a.b.Foo` flows through the
external-module path in `ImportProcessor._resolve_module_path` and
produces a synthetic `Module` node — even though `Foo.kt` already exists
in the graph.

This helper walks every `.kt` and `.kts` file in the repo, parses each
with tree-sitter-kotlin, reads its `package` declaration, and records
each top-level declaration under two keys — the package-form name a
Kotlin import statement uses, and the canonical file-path-rooted QN the
ingestion pipeline produces. Both map to the same
`(canonical_qn, module_qn)` tuple, so callers can resolve in either
direction without re-deriving the Module QN by string arithmetic
(which is wrong for top-level extension functions whose canonical QN
carries a receiver prefix — `<module>.<receiver>.<name>`).
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
) -> dict[str, tuple[str, str]]:
    parser = _make_kotlin_parser()
    if parser is None:
        return {}
    index: dict[str, tuple[str, str]] = {}
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
            for simple_name, qn_segment in _iter_top_level_named_decls(
                tree.root_node
            ):
                canonical_qn = f"{module_qn}{cs.SEPARATOR_DOT}{qn_segment}"
                entry = (canonical_qn, module_qn)
                index[f"{package}{cs.SEPARATOR_DOT}{simple_name}"] = entry
                index[canonical_qn] = entry
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


def _iter_top_level_named_decls(root_node) -> list[tuple[str, str]]:
    """Yield (simple_name, qn_segment) for each top-level declaration.

    `simple_name` is what appears in a Kotlin import statement
    (`import pkg.<simple_name>`); `qn_segment` is the trailing part of the
    canonical QN that ingestion produces. The two diverge for top-level
    extension functions: `fun String.shout()` is imported as `shout` but
    ingested under `<module>.String.shout`, so its `qn_segment` carries
    the receiver prefix.
    """
    entries: list[tuple[str, str]] = []
    for child in root_node.children:
        if child.type not in _TOP_LEVEL_NAMED_DECL_TYPES:
            continue
        field = (
            cs.TS_FIELD_TYPE
            if child.type == cs.TS_KOTLIN_TYPE_ALIAS
            else cs.TS_FIELD_NAME
        )
        name_node = child.child_by_field_name(field)
        if name_node is None or name_node.text is None:
            continue
        simple_name = name_node.text.decode(cs.ENCODING_UTF8)
        qn_segment = simple_name
        if child.type == cs.TS_KOTLIN_FUNCTION_DECLARATION:
            receiver = kotlin_utils.extract_receiver_type(child)
            if receiver:
                qn_segment = f"{receiver}{cs.SEPARATOR_DOT}{simple_name}"
        entries.append((simple_name, qn_segment))
    return entries


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
