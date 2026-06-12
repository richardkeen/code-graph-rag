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
from collections.abc import Iterator
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
    extension_prefixes_by_package: dict[str, set[str]]


def build_kotlin_package_index(
    repo_path: Path, project_name: str
) -> KotlinPackageIndex:
    parser = _make_kotlin_parser()
    if parser is None:
        return KotlinPackageIndex(
            by_name={},
            modules_by_package={},
            module_qns=set(),
            extension_prefixes_by_package={},
        )
    by_name: dict[str, tuple[str, str]] = {}
    modules_by_package: dict[str, list[str]] = {}
    seen_module_per_package: dict[str, set[str]] = {}
    module_qns: set[str] = set()
    extension_prefixes_by_package: dict[str, set[str]] = {}
    kotlin_extensions = set(cs.KOTLIN_EXTENSIONS)
    for file_path in repo_path.rglob("*"):
        if file_path.suffix not in kotlin_extensions:
            continue
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
        for import_path, qn_segment in _iter_named_decls(tree.root_node):
            canonical_qn = f"{module_qn}{cs.SEPARATOR_DOT}{qn_segment}"
            entry = (canonical_qn, module_qn)
            by_name[f"{package}{cs.SEPARATOR_DOT}{import_path}"] = entry
            by_name[canonical_qn] = entry
            if qn_segment != import_path:
                receiver_prefix = canonical_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
                extension_prefixes_by_package.setdefault(
                    package, set()
                ).add(receiver_prefix)
                by_name[receiver_prefix] = (receiver_prefix, module_qn)
    return KotlinPackageIndex(
        by_name=by_name,
        modules_by_package=modules_by_package,
        module_qns=module_qns,
        extension_prefixes_by_package=extension_prefixes_by_package,
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


_NAMED_DECL_TYPES = frozenset(
    {
        cs.TS_KOTLIN_CLASS_DECLARATION,
        cs.TS_KOTLIN_OBJECT_DECLARATION,
        cs.TS_KOTLIN_FUNCTION_DECLARATION,
        cs.TS_KOTLIN_TYPE_ALIAS,
    }
)
_CONTAINER_DECL_TYPES = frozenset(
    {
        cs.TS_KOTLIN_CLASS_DECLARATION,
        cs.TS_KOTLIN_OBJECT_DECLARATION,
    }
)


def _iter_named_decls(
    parent_node, segment_prefix: str = ""
) -> Iterator[tuple[str, str]]:
    """Yield (import_path, qn_segment) for every named declaration.

    Walks `parent_node.children` and descends into class/object bodies so
    nested declarations like `class Outer { class Inner }` produce both
    an `Outer` entry and an `Outer.Inner` entry — matching how Kotlin
    imports name them (`import pkg.Outer.Inner`) and how ingestion builds
    the canonical QN (`<module>.Outer.Inner`).

    `import_path` is the dotted form a Kotlin import statement uses
    (`Outer.Inner`); `qn_segment` is the trailing part of the canonical
    QN that ingestion produces. The two diverge for top-level extension
    functions whose canonical QN carries a receiver prefix
    (`String.shout`); member functions inside class bodies are skipped
    because they aren't standalone-importable.
    """
    for child in parent_node.children:
        if child.type not in _NAMED_DECL_TYPES:
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
        if child.type == cs.TS_KOTLIN_FUNCTION_DECLARATION:
            if segment_prefix:
                continue
            receiver = kotlin_utils.extract_receiver_type(child)
            qn_segment = (
                f"{receiver}{cs.SEPARATOR_DOT}{simple_name}"
                if receiver
                else simple_name
            )
            yield simple_name, qn_segment
            continue
        nested_path = f"{segment_prefix}{simple_name}"
        yield nested_path, nested_path
        if child.type in _CONTAINER_DECL_TYPES:
            body = kotlin_utils.find_class_body(child)
            if body is not None:
                yield from _iter_named_decls(
                    body, f"{nested_path}{cs.SEPARATOR_DOT}"
                )


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
