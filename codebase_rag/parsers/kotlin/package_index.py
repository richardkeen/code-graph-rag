"""Bridge Kotlin package-form names and file-path-rooted Module/Class QNs.

Kotlin imports are written in package form (`import a.b.Foo`,
`import a.b.*`) but Modules / Classes / top-level functions are ingested
with file-path-rooted qualified names (`<project>.<relative_file_path>.<symbol>`).
Without a bridge, package-form references flow through the external-module
path in `ImportProcessor._resolve_module_path` and produce synthetic
`Module` nodes even when the real ones exist in the graph.

This module walks every `.kt` and `.kts` file in the repo, parses each
with tree-sitter-kotlin, and returns three coupled views built from the
same scan:

  - `by_name`: keyed by both `<package>.<simple_name>` and the canonical
    QN, valued as `(canonical_qn, module_qn)`. Lets callers resolve in
    either direction without re-deriving the Module QN by string
    arithmetic (which is wrong for top-level extension functions whose
    canonical QN carries a receiver prefix — `<module>.<receiver>.<name>`).
  - `modules_by_package`: `package → list[module_qn]`. Backs `import a.b.*`
    expansion: every internal Module declaring `package a.b` becomes a
    separate import entry / IMPORTS edge target.
  - `module_qns`: `set[str]` of every internal Kotlin Module QN. Lets
    `_resolve_module_path` short-circuit before the generic stdlib
    fallback strips the trailing uppercase segment of `<project>.Util`
    and emits a synthetic external `Module('<project>')`.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Language, Parser

from ... import constants as cs
from ...utils.path_utils import should_skip_path
from . import utils as kotlin_utils


class KotlinPackageIndex(NamedTuple):
    by_name: dict[str, tuple[str, str]]
    modules_by_package: dict[str, list[str]]
    module_qns: set[str]


def build_kotlin_package_index(
    repo_path: Path, project_name: str
) -> KotlinPackageIndex:
    parser = _make_kotlin_parser()
    if parser is None:
        return KotlinPackageIndex(by_name={}, modules_by_package={}, module_qns=set())
    by_name: dict[str, tuple[str, str]] = {}
    modules_by_package: dict[str, list[str]] = {}
    seen_module_per_package: dict[str, set[str]] = {}
    module_qns: set[str] = set()
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
            module_qns.add(module_qn)
            seen = seen_module_per_package.setdefault(package, set())
            if module_qn not in seen:
                modules_by_package.setdefault(package, []).append(module_qn)
                seen.add(module_qn)
            for simple_name, qn_segment in _iter_top_level_named_decls(
                tree.root_node
            ):
                canonical_qn = f"{module_qn}{cs.SEPARATOR_DOT}{qn_segment}"
                entry = (canonical_qn, module_qn)
                by_name[f"{package}{cs.SEPARATOR_DOT}{simple_name}"] = entry
                by_name[canonical_qn] = entry
    return KotlinPackageIndex(
        by_name=by_name,
        modules_by_package=modules_by_package,
        module_qns=module_qns,
    )


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
