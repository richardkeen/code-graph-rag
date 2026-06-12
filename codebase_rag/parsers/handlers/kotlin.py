from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ... import constants as cs
from ..kotlin import utils as kotlin_utils
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode
    from ..class_ingest.mixin import ClassIngestMixin


class KotlinHandler(BaseLanguageHandler):
    # Class-like nodes whose presence between a captured method and its
    # enclosing class means the method belongs to a nested container, not
    # the class currently being walked.
    _CLASS_LIKE_TYPES: ClassVar[frozenset[str]] = frozenset(
        {
            cs.TS_KOTLIN_CLASS_DECLARATION,
            cs.TS_KOTLIN_OBJECT_DECLARATION,
            cs.TS_KOTLIN_COMPANION_OBJECT,
        }
    )
    # Function-like nodes whose presence between a captured function and
    # its enclosing class means the captured node is a local declaration
    # inside that function — not a class method.
    _FUNCTION_LIKE_TYPES: ClassVar[frozenset[str]] = frozenset(
        {
            cs.TS_KOTLIN_FUNCTION_DECLARATION,
            cs.TS_KOTLIN_PRIMARY_CONSTRUCTOR,
            cs.TS_KOTLIN_SECONDARY_CONSTRUCTOR,
            cs.TS_KOTLIN_ANONYMOUS_FUNCTION,
            cs.TS_KOTLIN_LAMBDA_LITERAL,
            cs.TS_KOTLIN_GETTER,
            cs.TS_KOTLIN_SETTER,
        }
    )

    def extract_decorators(self, node: ASTNode) -> list[str]:
        return kotlin_utils.extract_annotations(node)

    def find_class_body(self, class_node: ASTNode) -> ASTNode | None:
        # tree-sitter-kotlin places `primary_constructor` as a sibling of
        # `class_body` rather than a descendant, and a class with only an
        # inline primary constructor (`class Person(val name: String)`)
        # has no `class_body` at all. Returning `class_node` is the
        # smallest scope that covers every direct member; nested-class
        # captures are filtered by `is_direct_class_member` below.
        return class_node

    def is_direct_class_member(self, method_node: ASTNode, class_node: ASTNode) -> bool:
        """Return True iff the nearest class-like ancestor of method_node is class_node.

        The Kotlin function query is unanchored, so when running it against an outer
        class's scope it also captures functions inside nested companions / objects /
        classes, and *local* functions declared inside method bodies / lambdas /
        accessors / constructors. Two failure modes to reject:

          1. Nested-container leakage — a function inside a nested companion /
             object / inner class. We bail when a class-like ancestor sits between
             method_node and class_node.
          2. Local-function leakage — a function declared inside another function's
             body. Local declarations are not class methods; we bail when a
             function-like ancestor sits between method_node and class_node.

        Compare by tree-sitter node `id` because the Python bindings wrap each
        `.parent` lookup in a fresh object — `is` and `==` are not reliable.
        """
        target_id = class_node.id
        current = method_node.parent
        while current is not None:
            if current.id == target_id:
                return True
            if current.type in self._CLASS_LIKE_TYPES:
                return False
            if current.type in self._FUNCTION_LIKE_TYPES:
                return False
            current = current.parent
        return False

    def extract_method_name(self, method_node: ASTNode) -> str | None:
        return kotlin_utils.extract_function_info(method_node).name

    def build_method_qualified_name(
        self,
        class_qn: str,
        method_name: str,
        method_node: ASTNode,
    ) -> str:
        # class_qn is already the immediately enclosing class's fully-qualified name
        # (the mixin filters out methods inside nested classes/companions). For a
        # companion object that resolves to ".../OuterClass.Companion", so we simply
        # append the method name and parameter signature without any extra routing.
        info = kotlin_utils.extract_function_info(method_node)
        base = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        if info.parameters:
            param_sig = cs.SEPARATOR_COMMA_SPACE.join(info.parameters)
            return f"{base}({param_sig})"
        return base

    def build_caller_qn(
        self,
        class_qn: str,
        method_name: str,
        method_node: ASTNode,
    ) -> str:
        return self.build_method_qualified_name(class_qn, method_name, method_node)

    @property
    def calls_fqn_spec(self) -> object:
        from ...language_spec import KOTLIN_FQN_SPEC

        return KOTLIN_FQN_SPEC

    def finalize_post_passes(self, processor: ClassIngestMixin) -> None:
        """Resolve Kotlin parent QNs and emit INHERITS / IMPLEMENTS edges.

        Kotlin's `delegation_specifiers` grammar fuses class inheritance
        with interface implementation, so the edge type cannot be decided
        while parsing each file. This deferred pass runs once Pass 2 has
        registered every parent's NodeType, rewrites parent QNs in
        `pending_inheritance` to their canonical registry-rooted form
        (visible to the override walker via the shared list reference set
        up in `relationships.create_class_relationships`), and emits the
        resolved edges.
        """
        from ..class_ingest import kotlin_inheritance as ki

        ki.process_all_kotlin_inheritance_edges(
            processor.function_registry,
            processor.pending_inheritance,
            processor.simple_name_lookup,
            processor.ingestor,
        )
