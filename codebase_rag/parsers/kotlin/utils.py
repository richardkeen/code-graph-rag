from __future__ import annotations

from typing import NamedTuple

from ... import constants as cs
from ...types_defs import ASTNode
from ..utils import safe_decode_text


class KotlinFunctionInfo(NamedTuple):
    name: str | None
    parameters: list[str]
    receiver_type: str | None
    modifiers: list[str]
    annotations: list[str]


class KotlinImport(NamedTuple):
    path: str
    alias: str | None
    is_wildcard: bool


_MODIFIER_GROUP_TYPES = frozenset(
    {
        cs.TS_KOTLIN_CLASS_MODIFIER,
        cs.TS_KOTLIN_FUNCTION_MODIFIER,
        cs.TS_KOTLIN_VISIBILITY_MODIFIER,
        cs.TS_KOTLIN_INHERITANCE_MODIFIER,
        cs.TS_KOTLIN_MEMBER_MODIFIER,
        cs.TS_KOTLIN_PARAMETER_MODIFIER,
        cs.TS_KOTLIN_PROPERTY_MODIFIER,
        cs.TS_KOTLIN_PLATFORM_MODIFIER,
    }
)


def _decode_qualified_identifier(node: ASTNode) -> str | None:
    """Join the `identifier` children of a qualified_identifier with dots.

    The tree-sitter-kotlin grammar wraps dotted names like
    `kotlin.collections` in a single `qualified_identifier` node containing
    alternating `identifier` and `.` children — we only care about the named
    identifier parts.
    """
    parts: list[str] = []
    for child in node.children:
        if child.type == cs.TS_KOTLIN_IDENTIFIER and (text := safe_decode_text(child)):
            parts.append(text)
    return cs.SEPARATOR_DOT.join(parts) if parts else None


def extract_package_name(root_node: ASTNode) -> str | None:
    for child in root_node.children:
        if child.type != cs.TS_KOTLIN_PACKAGE_HEADER:
            continue
        for header_child in child.children:
            if header_child.type == cs.TS_KOTLIN_QUALIFIED_IDENTIFIER:
                return _decode_qualified_identifier(header_child)
    return None


def extract_imports(root_node: ASTNode) -> list[KotlinImport]:
    imports: list[KotlinImport] = []
    for child in root_node.children:
        if child.type != cs.TS_KOTLIN_IMPORT:
            continue
        if imp := parse_import_header(child):
            imports.append(imp)
    return imports


def parse_import_header(import_node: ASTNode) -> KotlinImport | None:
    """Parse one `import` declaration into (path, optional alias, wildcard?).

    tree-sitter-kotlin shape: an `import` named-node whose children are the
    literal `import` keyword, a `qualified_identifier` for the path, and then
    either:
      - a literal `.` and `*` for wildcard imports, or
      - a literal `as` keyword and a final `identifier` for aliased imports.
    """
    path: str | None = None
    alias: str | None = None
    is_wildcard = False
    seen_qualified = False
    for child in import_node.children:
        if child.type == cs.TS_KOTLIN_QUALIFIED_IDENTIFIER:
            path = _decode_qualified_identifier(child)
            seen_qualified = True
        elif child.type == "*":
            is_wildcard = True
        elif (
            seen_qualified
            and child.type == cs.TS_KOTLIN_IDENTIFIER
            and (text := safe_decode_text(child))
        ):
            alias = text
    if not path:
        return None
    return KotlinImport(path=path, alias=alias, is_wildcard=is_wildcard)


def _modifiers_node(node: ASTNode) -> ASTNode | None:
    for child in node.children:
        if child.type == cs.TS_KOTLIN_MODIFIERS:
            return child
    return None


def extract_modifiers(node: ASTNode) -> list[str]:
    modifiers_node = _modifiers_node(node)
    if not modifiers_node:
        return []
    out: list[str] = []
    for child in modifiers_node.children:
        if child.type in _MODIFIER_GROUP_TYPES and (text := safe_decode_text(child)):
            out.append(text)
    return out


def extract_class_modifiers(class_node: ASTNode) -> list[str]:
    return extract_modifiers(class_node)


def is_kotlin_interface(class_node: ASTNode) -> bool:
    """Distinguish Kotlin `interface Foo {}` from `class Foo {}`.

    tree-sitter-kotlin uses the same `class_declaration` node type for both;
    the only structural difference is whether the node has an `interface`
    or `class` keyword as a direct child.
    """
    for child in class_node.children:
        if child.type == cs.TS_KOTLIN_INTERFACE_KEYWORD:
            return True
    return False


def _user_type_name(user_type_node: ASTNode) -> str | None:
    for child in user_type_node.children:
        if child.type == cs.TS_KOTLIN_IDENTIFIER and (text := safe_decode_text(child)):
            return text
    return None


def extract_annotations(node: ASTNode) -> list[str]:
    """Return annotation names attached to a declaration, e.g. `["Service", "JvmInline"]`."""
    modifiers_node = _modifiers_node(node)
    if not modifiers_node:
        return []
    annotations: list[str] = []
    for child in modifiers_node.children:
        if child.type != cs.TS_KOTLIN_ANNOTATION:
            continue
        for ann_child in child.children:
            if ann_child.type == cs.TS_KOTLIN_USER_TYPE:
                if name := _user_type_name(ann_child):
                    annotations.append(name)
            elif ann_child.type == cs.TS_KOTLIN_CONSTRUCTOR_INVOCATION:
                for ci_child in ann_child.children:
                    if ci_child.type == cs.TS_KOTLIN_USER_TYPE:
                        if name := _user_type_name(ci_child):
                            annotations.append(name)
    return annotations


def _type_node_name(type_node: ASTNode) -> str | None:
    """Walk a `type` / `user_type` / `nullable_type` node to its leaf type name."""
    for child in type_node.children:
        if child.type == cs.TS_KOTLIN_USER_TYPE:
            if name := _user_type_name(child):
                return name
        elif child.type == cs.TS_KOTLIN_NULLABLE_TYPE:
            if name := _type_node_name(child):
                return f"{name}?"
        elif child.type == cs.TS_KOTLIN_IDENTIFIER and (
            text := safe_decode_text(child)
        ):
            return text
    return None


def _parameter_type_name(param_node: ASTNode) -> str | None:
    """Return the declared type identifier for a `parameter` or `class_parameter`.

    Both node shapes contain an `identifier` (the parameter name) followed by
    a `type` / `user_type` describing its declared type. We skip the leading
    name identifier and read the first type-bearing child.
    """
    seen_name = False
    for child in param_node.children:
        if child.type == cs.TS_KOTLIN_IDENTIFIER and not seen_name:
            seen_name = True
            continue
        if child.type in (cs.TS_KOTLIN_TYPE, cs.TS_KOTLIN_USER_TYPE):
            if name := _type_node_name(child):
                return name
        elif child.type == cs.TS_KOTLIN_NULLABLE_TYPE:
            # _type_node_name already appends "?" for nullable types; don't add another.
            return _type_node_name(child)
    return None


def _extract_parameters(fn_node: ASTNode) -> list[str]:
    """Collect declared parameter types for a function or constructor.

    Regular functions and secondary constructors expose their params via a
    `function_value_parameters` wrapper that contains `parameter` children;
    primary constructors wrap `class_parameter` children in a
    `class_parameters` (plural) node.
    """
    params: list[str] = []
    for child in fn_node.children:
        if child.type == cs.TS_KOTLIN_FUNCTION_VALUE_PARAMETERS:
            for grand in child.children:
                if grand.type == cs.TS_KOTLIN_PARAMETER:
                    if t := _parameter_type_name(grand):
                        params.append(t)
        elif child.type == cs.TS_KOTLIN_CLASS_PARAMETERS:
            for grand in child.children:
                if grand.type == cs.TS_KOTLIN_CLASS_PARAMETER:
                    if t := _parameter_type_name(grand):
                        params.append(t)
    return params


def _extract_property_name(accessor_node: ASTNode) -> str | None:
    """Return the property name owning a getter or setter node.

    The accessor is a direct child of `property_declaration`, which in turn
    has a `variable_declaration` child whose first `identifier` child is the
    property name.
    """
    prop = accessor_node.parent
    if prop is None or prop.type != cs.TS_KOTLIN_PROPERTY_DECLARATION:
        return None
    for child in prop.children:
        if child.type == cs.TS_KOTLIN_VARIABLE_DECLARATION:
            for grandchild in child.children:
                if grandchild.type == cs.TS_KOTLIN_IDENTIFIER and grandchild.text:
                    return safe_decode_text(grandchild)
    return None


def _extract_function_name(fn_node: ASTNode) -> str | None:
    if fn_node.type == cs.TS_KOTLIN_FUNCTION_DECLARATION:
        if (name := fn_node.child_by_field_name(cs.TS_FIELD_NAME)) and name.text:
            return safe_decode_text(name)
        return None
    if fn_node.type == cs.TS_KOTLIN_GETTER:
        prop = _extract_property_name(fn_node)
        return f"{prop}<getter>" if prop else "<getter>"
    if fn_node.type == cs.TS_KOTLIN_SETTER:
        prop = _extract_property_name(fn_node)
        return f"{prop}<setter>" if prop else "<setter>"
    if fn_node.type in (
        cs.TS_KOTLIN_PRIMARY_CONSTRUCTOR,
        cs.TS_KOTLIN_SECONDARY_CONSTRUCTOR,
    ):
        return "<init>"
    return None


def extract_receiver_type(fn_node: ASTNode) -> str | None:
    """For `fun String.shout()` return "String".

    tree-sitter-kotlin emits the receiver as a `user_type` positional child
    appearing *before* the `name` field's `identifier` child, with a literal
    `.` between them. We treat any `user_type` that precedes the name node
    as the receiver; a `user_type` after the name is the return type.
    """
    if fn_node.type != cs.TS_KOTLIN_FUNCTION_DECLARATION:
        return None
    name_node = fn_node.child_by_field_name(cs.TS_FIELD_NAME)
    if not name_node:
        return None
    receiver: ASTNode | None = None
    for child in fn_node.children:
        if child.id == name_node.id:
            break
        if child.type == cs.TS_KOTLIN_USER_TYPE:
            receiver = child
    return _user_type_name(receiver) if receiver else None


def extract_function_info(fn_node: ASTNode) -> KotlinFunctionInfo:
    return KotlinFunctionInfo(
        name=_extract_function_name(fn_node),
        parameters=_extract_parameters(fn_node),
        receiver_type=extract_receiver_type(fn_node),
        modifiers=extract_modifiers(fn_node),
        annotations=extract_annotations(fn_node),
    )


def is_companion_object(node: ASTNode) -> bool:
    return node.type == cs.TS_KOTLIN_COMPANION_OBJECT


def find_class_body(class_node: ASTNode) -> ASTNode | None:
    for child in class_node.children:
        if child.type in (
            cs.TS_KOTLIN_CLASS_BODY,
            cs.TS_KOTLIN_ENUM_CLASS_BODY,
        ):
            return child
    return None
