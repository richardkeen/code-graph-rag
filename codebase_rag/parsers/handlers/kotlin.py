from __future__ import annotations

from typing import TYPE_CHECKING

from ... import constants as cs
from ..kotlin import utils as kotlin_utils
from .base import BaseLanguageHandler

if TYPE_CHECKING:
    from ...types_defs import ASTNode


class KotlinHandler(BaseLanguageHandler):
    def extract_decorators(self, node: ASTNode) -> list[str]:
        return kotlin_utils.extract_annotations(node)

    def find_class_body(self, class_node: ASTNode) -> ASTNode | None:
        return kotlin_utils.find_class_body(class_node)

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
