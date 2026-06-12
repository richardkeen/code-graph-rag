from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs
from ..cpp import utils as cpp_utils
from ..utils import safe_decode_text
from .utils import find_child_by_type

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor


def extract_parent_classes(
    class_node: Node,
    module_qn: str,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    if class_node.type in cs.CPP_CLASS_TYPES:
        return extract_cpp_parent_classes(class_node, module_qn)

    parent_classes: list[str] = []

    if class_node.type == cs.TS_CLASS_DECLARATION:
        parent_classes.extend(
            extract_java_superclass(class_node, module_qn, resolve_to_qn)
        )

    if class_node.type in (
        cs.TS_KOTLIN_CLASS_DECLARATION,
        cs.TS_KOTLIN_OBJECT_DECLARATION,
        cs.TS_KOTLIN_COMPANION_OBJECT,
    ):
        parent_classes.extend(
            extract_kotlin_supertypes(class_node, module_qn, resolve_to_qn)
        )

    parent_classes.extend(
        extract_python_superclasses(
            class_node, module_qn, import_processor, resolve_to_qn
        )
    )

    if class_heritage_node := find_child_by_type(class_node, cs.TS_CLASS_HERITAGE):
        parent_classes.extend(
            extract_js_ts_heritage_parents(
                class_heritage_node, module_qn, import_processor, resolve_to_qn
            )
        )

    if class_node.type == cs.TS_INTERFACE_DECLARATION:
        parent_classes.extend(
            extract_interface_parents(
                class_node, module_qn, import_processor, resolve_to_qn
            )
        )

    return parent_classes


def extract_cpp_parent_classes(class_node: Node, module_qn: str) -> list[str]:
    parent_classes: list[str] = []
    for child in class_node.children:
        if child.type == cs.TS_BASE_CLASS_CLAUSE:
            parent_classes.extend(parse_cpp_base_classes(child, class_node, module_qn))
    return parent_classes


def parse_cpp_base_classes(
    base_clause_node: Node, class_node: Node, module_qn: str
) -> list[str]:
    parent_classes: list[str] = []
    base_type_nodes = (
        cs.TS_TYPE_IDENTIFIER,
        cs.CppNodeType.QUALIFIED_IDENTIFIER,
        cs.TS_TEMPLATE_TYPE,
    )

    for base_child in base_clause_node.children:
        if base_child.type in (
            cs.TS_ACCESS_SPECIFIER,
            cs.TS_VIRTUAL,
            cs.CHAR_COMMA,
            cs.CHAR_COLON,
        ):
            continue

        if base_child.type in base_type_nodes and base_child.text:
            if parent_name := safe_decode_text(base_child):
                base_name = extract_cpp_base_class_name(parent_name)
                parent_qn = cpp_utils.build_qualified_name(
                    class_node, module_qn, base_name
                )
                parent_classes.append(parent_qn)
                logger.debug(
                    logs.CLASS_CPP_INHERITANCE.format(
                        parent_name=parent_name, parent_qn=parent_qn
                    )
                )

    return parent_classes


def extract_cpp_base_class_name(parent_text: str) -> str:
    if cs.CHAR_ANGLE_OPEN in parent_text:
        parent_text = parent_text.split(cs.CHAR_ANGLE_OPEN)[0]

    if cs.SEPARATOR_DOUBLE_COLON in parent_text:
        parent_text = parent_text.split(cs.SEPARATOR_DOUBLE_COLON)[-1]

    return parent_text


def resolve_superclass_from_type_identifier(
    type_identifier_node: Node,
    module_qn: str,
    resolve_to_qn: Callable[[str, str], str],
) -> str | None:
    if type_identifier_node.text:
        if parent_name := safe_decode_text(type_identifier_node):
            return resolve_to_qn(parent_name, module_qn)
    return None


def extract_java_superclass(
    class_node: Node,
    module_qn: str,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    superclass_node = class_node.child_by_field_name(cs.FIELD_SUPERCLASS)
    if not superclass_node:
        return []

    if superclass_node.type == cs.TS_TYPE_IDENTIFIER:
        if resolved := resolve_superclass_from_type_identifier(
            superclass_node, module_qn, resolve_to_qn
        ):
            return [resolved]
        return []

    for child in superclass_node.children:
        if child.type == cs.TS_TYPE_IDENTIFIER:
            if resolved := resolve_superclass_from_type_identifier(
                child, module_qn, resolve_to_qn
            ):
                return [resolved]
    return []


def extract_kotlin_supertypes(
    class_node: Node,
    module_qn: str,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    """Walk Kotlin's `delegation_specifiers` block for INHERITS / IMPLEMENTS targets.

    tree-sitter-kotlin wraps the parent list in `delegation_specifiers` (plural).
    Each `delegation_specifier` child holds one of:
      - `constructor_invocation` (e.g. `: Base()`) → the supertype is the
        first `user_type` child of the invocation.
      - `user_type` directly (e.g. `: Iface`) → take its identifier.
      - `explicit_delegation` (e.g. `: Iface by impl`) → first `user_type`
        child names the interface being delegated.
    """
    parents: list[str] = []
    for child in class_node.children:
        if child.type != cs.TS_KOTLIN_DELEGATION_SPECIFIERS:
            continue
        for spec in child.children:
            if spec.type != cs.TS_KOTLIN_DELEGATION_SPECIFIER:
                continue
            for grand in spec.children:
                if grand.type == cs.TS_KOTLIN_CONSTRUCTOR_INVOCATION:
                    for ci_child in grand.children:
                        if ci_child.type == cs.TS_KOTLIN_USER_TYPE:
                            if name := _kotlin_user_type_name(ci_child):
                                parents.append(resolve_to_qn(name, module_qn))
                            break
                elif grand.type == cs.TS_KOTLIN_USER_TYPE:
                    if name := _kotlin_user_type_name(grand):
                        parents.append(resolve_to_qn(name, module_qn))
                elif grand.type == cs.TS_KOTLIN_EXPLICIT_DELEGATION:
                    for dch in grand.children:
                        if dch.type == cs.TS_KOTLIN_USER_TYPE:
                            if name := _kotlin_user_type_name(dch):
                                parents.append(resolve_to_qn(name, module_qn))
                            break
    return parents


def _kotlin_user_type_name(user_type_node: Node) -> str | None:
    """Reconstruct the qualified Kotlin type name from a `user_type` node.

    Kotlin parents like `class C : foo.bar.Base()` or `class C : Outer.Inner()`
    are represented as a `user_type` node containing several `identifier`
    children separated by literal `.` tokens. Returning only the first
    identifier loses the qualifier (`foo` for `foo.bar.Base`, `Outer` for
    `Outer.Inner`) and breaks INHERITS/IMPLEMENTS resolution. Walk every
    identifier child and rejoin them so the full dotted name flows through
    to `_resolve_kotlin_parent`'s simple-name fallback.
    """
    parts = [
        text
        for child in user_type_node.children
        if child.type == cs.TS_KOTLIN_IDENTIFIER
        and (text := safe_decode_text(child))
    ]
    return cs.SEPARATOR_DOT.join(parts) if parts else None


def extract_python_superclasses(
    class_node: Node,
    module_qn: str,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    superclasses_node = class_node.child_by_field_name(cs.FIELD_SUPERCLASSES)
    if not superclasses_node:
        return []

    parent_classes: list[str] = []
    import_map = import_processor.import_mapping.get(module_qn)

    for child in superclasses_node.children:
        if child.type != cs.TS_IDENTIFIER or not child.text:
            continue
        if not (parent_name := safe_decode_text(child)):
            continue

        if import_map and parent_name in import_map:
            parent_classes.append(import_map[parent_name])
        elif import_map:
            parent_classes.append(resolve_to_qn(parent_name, module_qn))
        else:
            parent_classes.append(f"{module_qn}.{parent_name}")

    return parent_classes


def extract_js_ts_heritage_parents(
    class_heritage_node: Node,
    module_qn: str,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    parent_classes: list[str] = []

    for child in class_heritage_node.children:
        if child.type == cs.TS_EXTENDS_CLAUSE:
            parent_classes.extend(
                extract_from_extends_clause(
                    child, module_qn, import_processor, resolve_to_qn
                )
            )
            break
        if child.type in cs.JS_TS_PARENT_REF_TYPES:
            if is_preceded_by_extends(child, class_heritage_node):
                if parent_name := safe_decode_text(child):
                    parent_classes.append(
                        resolve_js_ts_parent_class(
                            parent_name, module_qn, import_processor, resolve_to_qn
                        )
                    )
        elif child.type == cs.TS_CALL_EXPRESSION:
            if is_preceded_by_extends(child, class_heritage_node):
                parent_classes.extend(
                    extract_mixin_parent_classes(
                        child, module_qn, import_processor, resolve_to_qn
                    )
                )

    return parent_classes


def extract_from_extends_clause(
    extends_clause: Node,
    module_qn: str,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    for grandchild in extends_clause.children:
        if grandchild.type in cs.JS_TS_PARENT_REF_TYPES:
            if parent_name := safe_decode_text(grandchild):
                return [
                    resolve_js_ts_parent_class(
                        parent_name, module_qn, import_processor, resolve_to_qn
                    )
                ]
    return []


def is_preceded_by_extends(child: Node, parent_node: Node) -> bool:
    child_index = parent_node.children.index(child)
    return (
        child_index > 0 and parent_node.children[child_index - 1].type == cs.TS_EXTENDS
    )


def extract_interface_parents(
    class_node: Node,
    module_qn: str,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    extends_clause = find_child_by_type(class_node, cs.TS_EXTENDS_TYPE_CLAUSE)
    if not extends_clause:
        return []

    parent_classes: list[str] = []
    for child in extends_clause.children:
        if child.type == cs.TS_TYPE_IDENTIFIER and child.text:
            if parent_name := safe_decode_text(child):
                parent_qn = resolve_js_ts_parent_class(
                    parent_name, module_qn, import_processor, resolve_to_qn
                )
                parent_classes.append(parent_qn)
                logger.debug(f"Interface {module_qn} extends {parent_qn}")
        elif child.type == cs.TS_GENERIC_TYPE:
            type_id = find_child_by_type(child, cs.TS_TYPE_IDENTIFIER)
            if type_id and type_id.text:
                if parent_name := safe_decode_text(type_id):
                    parent_qn = resolve_js_ts_parent_class(
                        parent_name, module_qn, import_processor, resolve_to_qn
                    )
                    parent_classes.append(parent_qn)
                    logger.debug(f"Interface {module_qn} extends generic {parent_qn}")
    return parent_classes


def extract_mixin_parent_classes(
    call_expr_node: Node,
    module_qn: str,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    parent_classes: list[str] = []

    for child in call_expr_node.children:
        if child.type == cs.TS_ARGUMENTS:
            for arg_child in child.children:
                if arg_child.type == cs.TS_IDENTIFIER and arg_child.text:
                    if parent_name := safe_decode_text(arg_child):
                        parent_classes.append(
                            resolve_js_ts_parent_class(
                                parent_name, module_qn, import_processor, resolve_to_qn
                            )
                        )
                elif arg_child.type == cs.TS_CALL_EXPRESSION:
                    parent_classes.extend(
                        extract_mixin_parent_classes(
                            arg_child, module_qn, import_processor, resolve_to_qn
                        )
                    )
            break

    return parent_classes


def resolve_js_ts_parent_class(
    parent_name: str,
    module_qn: str,
    import_processor: ImportProcessor,
    resolve_to_qn: Callable[[str, str], str],
) -> str:
    if module_qn not in import_processor.import_mapping:
        return f"{module_qn}.{parent_name}"
    import_map = import_processor.import_mapping[module_qn]
    if parent_name in import_map:
        return import_map[parent_name]
    return resolve_to_qn(parent_name, module_qn)


def extract_implemented_interfaces(
    class_node: Node,
    module_qn: str,
    resolve_to_qn: Callable[[str, str], str],
) -> list[str]:
    implemented_interfaces: list[str] = []

    interfaces_node = class_node.child_by_field_name(cs.FIELD_INTERFACES)
    if interfaces_node:
        extract_java_interface_names(
            interfaces_node, implemented_interfaces, module_qn, resolve_to_qn
        )

    if class_heritage_node := find_child_by_type(class_node, cs.TS_CLASS_HERITAGE):
        for child in class_heritage_node.children:
            if child.type == cs.TS_IMPLEMENTS_CLAUSE:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_TYPE_IDENTIFIER and grandchild.text:
                        if interface_name := safe_decode_text(grandchild):
                            interface_qn = resolve_to_qn(interface_name, module_qn)
                            implemented_interfaces.append(interface_qn)
                            logger.debug(f"Class {module_qn} implements {interface_qn}")

    return implemented_interfaces


def extract_java_interface_names(
    interfaces_node: Node,
    interface_list: list[str],
    module_qn: str,
    resolve_to_qn: Callable[[str, str], str],
) -> None:
    for child in interfaces_node.children:
        if child.type == cs.TS_TYPE_LIST:
            for type_child in child.children:
                if type_child.type == cs.TS_TYPE_IDENTIFIER and type_child.text:
                    if interface_name := safe_decode_text(type_child):
                        interface_list.append(resolve_to_qn(interface_name, module_qn))
