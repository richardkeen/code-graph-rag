from __future__ import annotations

import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..models import CallProcessingMetrics
from ..services import IngestorProtocol
from ..types_defs import (
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    NodeType,
    PropertyDict,
)
from .call_resolver import CallResolver
from .cpp import utils as cpp_utils
from .hash_generator import generate_unique_hash
from .import_processor import ImportProcessor
from .type_inference import TypeInferenceEngine
from .utils import get_function_captures, is_method_node

if TYPE_CHECKING:
    from .workspace.protocol import WorkspaceResolver


class CallProcessor:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
        workspace_resolver: WorkspaceResolver | None = None,
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name

        self._resolver = CallResolver(
            function_registry=function_registry,
            import_processor=import_processor,
            type_inference=type_inference,
            class_inheritance=class_inheritance,
            ingestor=ingestor,
            workspace_resolver=workspace_resolver,
        )
        self.metrics = CallProcessingMetrics()

    def _get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            if node.type == cs.TS_ARROW_FUNCTION and node.parent:
                parent = node.parent
                if parent.type == cs.TS_VARIABLE_DECLARATOR:
                    name_node = parent.child_by_field_name(cs.FIELD_NAME)
                    if name_node and name_node.text:
                        return name_node.text.decode(cs.ENCODING_UTF8)
            return None
        text = name_node.text
        return None if text is None else text.decode(cs.ENCODING_UTF8)

    def process_calls_in_file(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        self.metrics.files_attempted += 1
        relative_path = file_path.relative_to(self.repo_path)
        logger.debug(ls.CALL_PROCESSING_FILE.format(path=relative_path))

        module_qn = cs.SEPARATOR_DOT.join(
            [self.project_name] + list(relative_path.with_suffix("").parts)
        )
        if (
            file_path.name in (cs.INIT_PY, cs.MOD_RS)
            or file_path.name in cs.JS_TS_INDEX_FILENAMES
        ):
            module_qn = cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.parent.parts)
            )

        existing_hashes: set[str] = set()
        had_error = False
        resolved_before = self.metrics.calls_resolved

        try:
            self._process_calls_in_functions(
                root_node, module_qn, language, queries, existing_hashes
            )
        except Exception as e:
            had_error = True
            logger.error(
                ls.CALL_FUNCTIONS_FAILED.format(
                    path=file_path, error=e, trace=traceback.format_exc()
                )
            )

        try:
            self._process_calls_in_classes(root_node, module_qn, language, queries)
        except Exception as e:
            had_error = True
            logger.error(
                ls.CALL_CLASSES_FAILED.format(
                    path=file_path, error=e, trace=traceback.format_exc()
                )
            )

        try:
            self._process_module_level_calls(root_node, module_qn, language, queries)
        except Exception as e:
            had_error = True
            logger.error(
                ls.CALL_MODULE_LEVEL_FAILED.format(
                    path=file_path, error=e, trace=traceback.format_exc()
                )
            )

        if had_error:
            self.metrics.files_with_errors += 1
        if self.metrics.calls_resolved == resolved_before:
            self.metrics.files_with_zero_calls.append(str(file_path))

    def _process_calls_in_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        existing_hashes: set[str] | None = None,
    ) -> None:
        if existing_hashes is None:
            existing_hashes = set()
        result = get_function_captures(root_node, language, queries)
        if not result:
            return

        lang_config, captures = result
        func_nodes = captures.get(cs.CAPTURE_FUNCTION, [])
        for func_node in func_nodes:
            if not isinstance(func_node, Node):
                continue
            try:
                if self._is_method(func_node, lang_config):
                    continue

                if language == cs.SupportedLanguage.CPP:
                    func_name = cpp_utils.extract_function_name(func_node)
                else:
                    func_name = self._get_node_name(func_node)
                if not func_name:
                    if func_node.type == cs.TS_ARROW_FUNCTION and func_node.parent:
                        parent = func_node.parent
                        parent_type = parent.type

                        if parent_type == cs.TS_ARGUMENTS:
                            ancestor_qn = self._find_nearest_named_ancestor_qn(
                                func_node, module_qn, lang_config
                            )
                            if not ancestor_qn:
                                ancestor_qn = module_qn
                                ancestor_type = cs.NodeLabel.MODULE
                            else:
                                ancestor_type = cs.NodeLabel.FUNCTION

                            func_qn = self._create_anonymous_function_node(
                                func_node,
                                ancestor_qn,
                                ancestor_type,
                                module_qn,
                                existing_hashes,
                            )

                            self._ingest_function_calls(
                                func_node,
                                func_qn,
                                cs.NodeLabel.ANONYMOUS_FUNCTION,
                                module_qn,
                                language,
                                queries,
                            )
                            continue

                        if parent_type == cs.TS_RETURN_STATEMENT:
                            ancestor_qn = self._find_nearest_named_ancestor_qn(
                                func_node, module_qn, lang_config
                            )
                            if not ancestor_qn:
                                ancestor_qn = module_qn
                                ancestor_type = cs.NodeLabel.MODULE
                            else:
                                ancestor_type = cs.NodeLabel.FUNCTION

                            func_qn = self._create_anonymous_function_node(
                                func_node,
                                ancestor_qn,
                                ancestor_type,
                                module_qn,
                                existing_hashes,
                            )

                            self._ingest_function_calls(
                                func_node,
                                func_qn,
                                cs.NodeLabel.ANONYMOUS_FUNCTION,
                                module_qn,
                                language,
                                queries,
                            )
                            continue

                        if parent_type == cs.TS_PARENTHESIZED_EXPRESSION:
                            grandparent = parent.parent
                            if (
                                grandparent
                                and grandparent.type == cs.TS_RETURN_STATEMENT
                            ):
                                ancestor_qn = self._find_nearest_named_ancestor_qn(
                                    func_node, module_qn, lang_config
                                )
                                if not ancestor_qn:
                                    ancestor_qn = module_qn
                                    ancestor_type = cs.NodeLabel.MODULE
                                else:
                                    ancestor_type = cs.NodeLabel.FUNCTION

                                func_qn = self._create_anonymous_function_node(
                                    func_node,
                                    ancestor_qn,
                                    ancestor_type,
                                    module_qn,
                                    existing_hashes,
                                )

                                self._ingest_function_calls(
                                    func_node,
                                    func_qn,
                                    cs.NodeLabel.ANONYMOUS_FUNCTION,
                                    module_qn,
                                    language,
                                    queries,
                                )
                                continue

                        if parent_type == "pair":
                            key_node = parent.child_by_field_name("key")
                            if key_node:
                                property_name = (
                                    key_node.text.decode("utf8")
                                    if key_node.text
                                    else None
                                )
                                if property_name:
                                    property_name = property_name.strip("'\"")
                                    func_qn = self._build_nested_qualified_name(
                                        parent, module_qn, property_name, lang_config
                                    )
                                    if func_qn:
                                        logger.debug(
                                            f"Arrow as object property at line {func_node.start_point[0] + 1}: "
                                            f"name={property_name}, qn={func_qn}"
                                        )
                                        self._ingest_function_calls(
                                            func_node,
                                            func_qn,
                                            cs.NodeLabel.FUNCTION,
                                            module_qn,
                                            language,
                                            queries,
                                        )
                                        continue

                        if parent_type == "assignment_expression":
                            left_node = parent.child_by_field_name("left")
                            if left_node:
                                if left_node.type == "member_expression":
                                    property_node = left_node.child_by_field_name(
                                        "property"
                                    )
                                    if property_node and property_node.text:
                                        property_name = property_node.text.decode(
                                            "utf8"
                                        )
                                        func_qn = self._build_nested_qualified_name(
                                            parent,
                                            module_qn,
                                            property_name,
                                            lang_config,
                                        )
                                        if func_qn:
                                            logger.debug(
                                                f"Arrow in assignment at line {func_node.start_point[0] + 1}: "
                                                f"name={property_name}, qn={func_qn}"
                                            )
                                            self._ingest_function_calls(
                                                func_node,
                                                func_qn,
                                                cs.NodeLabel.FUNCTION,
                                                module_qn,
                                                language,
                                                queries,
                                            )
                                            continue
                                elif left_node.type == "identifier" and left_node.text:
                                    var_name = left_node.text.decode("utf8")
                                    func_qn = self._build_nested_qualified_name(
                                        parent, module_qn, var_name, lang_config
                                    )
                                    if func_qn:
                                        logger.debug(
                                            f"Arrow in assignment at line {func_node.start_point[0] + 1}: "
                                            f"name={var_name}, qn={func_qn}"
                                        )
                                        self._ingest_function_calls(
                                            func_node,
                                            func_qn,
                                            cs.NodeLabel.FUNCTION,
                                            module_qn,
                                            language,
                                            queries,
                                        )
                                        continue

                        context_prefix, method_name = self._detect_arrow_context(
                            func_node
                        )

                        if context_prefix in (
                            cs.ANON_PREFIX_JSX,
                            cs.ANON_PREFIX_TERNARY,
                        ):
                            ancestor_qn = self._find_nearest_named_ancestor_qn(
                                func_node, module_qn, lang_config
                            )
                            if not ancestor_qn:
                                ancestor_qn = module_qn
                                ancestor_type = cs.NodeLabel.MODULE
                            else:
                                ancestor_type = cs.NodeLabel.FUNCTION

                            func_qn = self._create_anonymous_function_node(
                                func_node,
                                ancestor_qn,
                                ancestor_type,
                                module_qn,
                                existing_hashes,
                            )

                            self._ingest_function_calls(
                                func_node,
                                func_qn,
                                cs.NodeLabel.ANONYMOUS_FUNCTION,
                                module_qn,
                                language,
                                queries,
                            )
                            continue

                        logger.debug(
                            f"Arrow function at line {func_node.start_point[0] + 1} has no name. "
                            f"Parent: {parent_type} (no handler implemented)"
                        )
                    continue
                if func_qn := self._build_nested_qualified_name(
                    func_node, module_qn, func_name, lang_config
                ):
                    if func_node.type == cs.TS_ARROW_FUNCTION:
                        logger.debug(
                            f"Processing arrow function: name={func_name}, qn={func_qn}, "
                            f"line={func_node.start_point[0] + 1}"
                        )
                    self._ingest_function_calls(
                        func_node,
                        func_qn,
                        cs.NodeLabel.FUNCTION,
                        module_qn,
                        language,
                        queries,
                    )
            except Exception as e:
                logger.error(
                    ls.CALL_FUNC_NODE_FAILED.format(
                        func_type=func_node.type,
                        line=func_node.start_point[0] + 1,
                        module=module_qn,
                        error=e,
                        trace=traceback.format_exc(),
                    )
                )

    def _find_nearest_named_ancestor_qn(
        self,
        node: Node,
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        current = node.parent
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                name = self._get_node_name(current)
                if name:
                    return self._build_nested_qualified_name(
                        current, module_qn, name, lang_config
                    )
            elif current.type in lang_config.class_node_types:
                return None
            elif current.type == cs.TS_VARIABLE_DECLARATOR:
                if name_node := current.child_by_field_name(cs.FIELD_NAME):
                    if name_node.text:
                        var_name = name_node.text.decode(cs.ENCODING_UTF8)
                        candidate_qn = f"{module_qn}{cs.SEPARATOR_DOT}{var_name}"
                        if candidate_qn in self._resolver.function_registry:
                            return candidate_qn
            current = current.parent
        return None

    def _detect_arrow_context(
        self,
        arrow_node: Node,
    ) -> tuple[str, str | None]:
        parent = arrow_node.parent
        if not parent:
            return (cs.ANON_PREFIX_GENERIC, None)

        parent_type = parent.type

        if parent_type == cs.TS_ARGUMENTS:
            call_parent = parent.parent
            if call_parent and call_parent.type == cs.TS_CALL_EXPRESSION:
                func_node = call_parent.child_by_field_name(cs.FIELD_FUNCTION)
                if func_node and func_node.type == cs.TS_MEMBER_EXPRESSION:
                    property_node = func_node.child_by_field_name(cs.FIELD_PROPERTY)
                    if property_node and property_node.text:
                        method_name = property_node.text.decode(cs.ENCODING_UTF8)
                        if method_name.startswith(cs.REACT_HOOK_PREFIX):
                            return (cs.ANON_PREFIX_HOOK, method_name)
                        if method_name in cs.ARRAY_PROMISE_METHODS:
                            return (cs.ANON_PREFIX_METHOD, method_name)

        if parent_type == cs.TS_RETURN_STATEMENT:
            return (cs.ANON_PREFIX_RETURN, None)
        if parent_type == cs.TS_PARENTHESIZED_EXPRESSION:
            grandparent = parent.parent
            if grandparent and grandparent.type == cs.TS_RETURN_STATEMENT:
                return (cs.ANON_PREFIX_RETURN, None)

        current = parent
        while current:
            if current.type == cs.TS_JSX_ATTRIBUTE:
                for child in current.children:
                    if child.type == cs.TS_PROPERTY_IDENTIFIER and child.text:
                        attr_name = child.text.decode(cs.ENCODING_UTF8)
                        if attr_name.startswith(cs.JSX_EVENT_PREFIX):
                            return (cs.ANON_PREFIX_JSX, attr_name)
                return (cs.ANON_PREFIX_JSX, None)
            if current.type in (cs.TS_JSX_ELEMENT, cs.TS_JSX_SELF_CLOSING_ELEMENT):
                break
            current = current.parent

        current = parent
        while current:
            if current.type in (cs.TS_TERNARY_EXPRESSION, cs.TS_CONDITIONAL_EXPRESSION):
                return (cs.ANON_PREFIX_TERNARY, None)
            if current.type in (
                cs.TS_STATEMENT_BLOCK,
                cs.TS_FUNCTION_DECLARATION,
                cs.TS_ARROW_FUNCTION,
            ):
                break
            current = current.parent

        return (cs.ANON_PREFIX_GENERIC, None)

    def _create_anonymous_function_node(
        self,
        arrow_node: Node,
        parent_qn: str,
        parent_type: str,
        module_qn: str,
        existing_hashes: set[str],
    ) -> str:
        context_prefix, method_name = self._detect_arrow_context(arrow_node)

        content_hash = generate_unique_hash(arrow_node, existing_hashes)
        existing_hashes.add(content_hash)

        if context_prefix == cs.ANON_PREFIX_METHOD and method_name:
            anon_name = f"{method_name}_{content_hash}"
            context_display = method_name
        elif context_prefix == cs.ANON_PREFIX_HOOK and method_name:
            anon_name = f"{cs.ANON_PREFIX_HOOK}_{method_name}_{content_hash}"
            context_display = f"{cs.ANON_PREFIX_HOOK}_{method_name}"
        elif context_prefix == cs.ANON_PREFIX_JSX and method_name:
            anon_name = f"{cs.ANON_PREFIX_JSX}_{method_name}_{content_hash}"
            context_display = f"{cs.ANON_PREFIX_JSX}_{method_name}"
        else:
            anon_name = f"{context_prefix}_{content_hash}"
            context_display = context_prefix

        anon_qn = f"{parent_qn}{cs.SEPARATOR_DOT}{anon_name}"

        anon_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: anon_qn,
            cs.KEY_NAME: anon_name,
            cs.KEY_START_LINE: arrow_node.start_point[0] + 1,
            cs.KEY_END_LINE: arrow_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: None,
        }

        logger.debug(ls.ANON_FUNC_CREATED.format(qn=anon_qn, context=context_display))

        self.ingestor.ensure_node_batch(cs.NodeLabel.ANONYMOUS_FUNCTION, anon_props)

        self.ingestor.ensure_relationship_batch(
            (parent_type, cs.KEY_QUALIFIED_NAME, parent_qn),
            cs.RelationshipType.DEFINES,
            (cs.NodeLabel.ANONYMOUS_FUNCTION, cs.KEY_QUALIFIED_NAME, anon_qn),
        )

        self._resolver.function_registry[anon_qn] = NodeType.ANONYMOUS_FUNCTION

        return anon_qn

    def _get_rust_impl_class_name(self, class_node: Node) -> str | None:
        class_name = self._get_node_name(class_node, cs.FIELD_TYPE)
        if class_name:
            return class_name
        return next(
            (
                child.text.decode(cs.ENCODING_UTF8)
                for child in class_node.children
                if child.type == cs.TS_TYPE_IDENTIFIER and child.is_named and child.text
            ),
            None,
        )

    def _get_class_name_for_node(
        self, class_node: Node, language: cs.SupportedLanguage
    ) -> str | None:
        if language == cs.SupportedLanguage.RUST and class_node.type == cs.TS_IMPL_ITEM:
            return self._get_rust_impl_class_name(class_node)
        return self._get_node_name(class_node)

    def _process_methods_in_class(
        self,
        body_node: Node,
        class_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        method_query = queries[language][cs.QUERY_FUNCTIONS]
        if not method_query:
            return
        method_cursor = QueryCursor(method_query)
        method_captures = method_cursor.captures(body_node)
        method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])
        for method_node in method_nodes:
            if not isinstance(method_node, Node):
                continue
            try:
                method_name = self._get_node_name(method_node)
                if not method_name:
                    continue
                method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
                self._ingest_function_calls(
                    method_node,
                    method_qn,
                    cs.NodeLabel.METHOD,
                    module_qn,
                    language,
                    queries,
                    class_qn,
                )
            except Exception as e:
                logger.error(
                    ls.CALL_METHOD_NODE_FAILED.format(
                        line=method_node.start_point[0] + 1,
                        class_qn=class_qn,
                        error=e,
                        trace=traceback.format_exc(),
                    )
                )

    def _process_calls_in_classes(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        query = queries[language][cs.QUERY_CLASSES]
        if not query:
            return
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        class_nodes = captures.get(cs.CAPTURE_CLASS, [])

        for class_node in class_nodes:
            if not isinstance(class_node, Node):
                continue
            try:
                class_name = self._get_class_name_for_node(class_node, language)
                if not class_name:
                    continue
                class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
                if body_node := class_node.child_by_field_name(cs.FIELD_BODY):
                    self._process_methods_in_class(
                        body_node, class_qn, module_qn, language, queries
                    )
            except Exception as e:
                logger.error(
                    ls.CALL_CLASS_NODE_FAILED.format(
                        line=class_node.start_point[0] + 1,
                        module=module_qn,
                        error=e,
                        trace=traceback.format_exc(),
                    )
                )

    def _process_module_level_calls(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        self._ingest_function_calls(
            root_node, module_qn, cs.NodeLabel.MODULE, module_qn, language, queries
        )

    def _get_call_target_name(self, call_node: Node) -> str | None:
        # Kotlin's call_expression and infix_expression have no tree-sitter fields
        # — they use positional children. JS/TS share the "call_expression" node
        # type string but expose a `function` field, so distinguish by absence of
        # field-based shape rather than node type alone.
        if (
            call_node.type
            in (cs.TS_KOTLIN_CALL_EXPRESSION, cs.TS_KOTLIN_INFIX_EXPRESSION)
            and call_node.child_by_field_name(cs.TS_FIELD_FUNCTION) is None
            and call_node.child_by_field_name(cs.FIELD_NAME) is None
        ):
            return self._get_kotlin_call_target_name(call_node)

        if func_child := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION):
            match func_child.type:
                case (
                    cs.TS_IDENTIFIER
                    | cs.TS_ATTRIBUTE
                    | cs.TS_MEMBER_EXPRESSION
                    | cs.CppNodeType.QUALIFIED_IDENTIFIER
                    | cs.TS_SCOPED_IDENTIFIER
                ):
                    if func_child.text is not None:
                        return str(func_child.text.decode(cs.ENCODING_UTF8))
                case cs.TS_CPP_FIELD_EXPRESSION:
                    field_node = func_child.child_by_field_name(cs.FIELD_FIELD)
                    if field_node and field_node.text:
                        return str(field_node.text.decode(cs.ENCODING_UTF8))
                case cs.TS_PARENTHESIZED_EXPRESSION:
                    return self._get_iife_target_name(func_child)

        match call_node.type:
            case (
                cs.TS_CPP_BINARY_EXPRESSION
                | cs.TS_CPP_UNARY_EXPRESSION
                | cs.TS_CPP_UPDATE_EXPRESSION
            ):
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    operator_text = operator_node.text.decode(cs.ENCODING_UTF8)
                    return cpp_utils.convert_operator_symbol_to_name(operator_text)
            case cs.TS_METHOD_INVOCATION:
                object_node = call_node.child_by_field_name(cs.FIELD_OBJECT)
                name_node = call_node.child_by_field_name(cs.FIELD_NAME)
                if name_node and name_node.text:
                    method_name = str(name_node.text.decode(cs.ENCODING_UTF8))
                    if not object_node or not object_node.text:
                        return method_name
                    object_text = str(object_node.text.decode(cs.ENCODING_UTF8))
                    return f"{object_text}{cs.SEPARATOR_DOT}{method_name}"

        if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
            if name_node.text is not None:
                return str(name_node.text.decode(cs.ENCODING_UTF8))

        return None

    def _get_kotlin_call_target_name(self, call_node: Node) -> str | None:
        """Walk a Kotlin call_expression / infix_expression and return the callee name.

        tree-sitter-kotlin call/infix shapes:
        - `(call_expression (identifier) value_arguments)` for direct calls like
          `User(1)` — the first named child is the callee identifier.
        - `(call_expression (navigation_expression expression "." identifier) value_arguments)`
          for method calls like `a.b.c()` — the trailing `identifier` child of the
          navigation_expression is the method name.
        - `(infix_expression expression identifier expression)` for `1 to 2`-style
          calls — the middle named child is the operator function.
        """
        if call_node.type == cs.TS_KOTLIN_CALL_EXPRESSION:
            for child in call_node.children:
                if not child.is_named:
                    continue
                if child.type == cs.TS_KOTLIN_IDENTIFIER and child.text:
                    return child.text.decode(cs.ENCODING_UTF8)
                if child.type == cs.TS_KOTLIN_NAVIGATION_EXPRESSION:
                    for grand in reversed(list(child.children)):
                        if grand.type == cs.TS_KOTLIN_IDENTIFIER and grand.text:
                            return grand.text.decode(cs.ENCODING_UTF8)
                    return None
                # First named child wasn't an identifier or navigation_expression.
                return None
            return None

        if call_node.type == cs.TS_KOTLIN_INFIX_EXPRESSION:
            named = [c for c in call_node.children if c.is_named]
            if len(named) >= 3:
                middle = named[1]
                if middle.type == cs.TS_KOTLIN_IDENTIFIER and middle.text:
                    return middle.text.decode(cs.ENCODING_UTF8)
            return None

        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        for child in parenthesized_expr.children:
            match child.type:
                case cs.TS_FUNCTION_EXPRESSION:
                    return f"{cs.IIFE_FUNC_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
                case cs.TS_ARROW_FUNCTION:
                    return f"{cs.IIFE_ARROW_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
        return None

    def _extract_function_reference_arguments(
        self, call_node: Node, language: cs.SupportedLanguage
    ) -> list[tuple[str, Node]]:
        references: list[tuple[str, Node]] = []

        args_node = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args_node:
            return references

        for child in args_node.children:
            if not child.is_named:
                continue
            references.extend(self._extract_identifiers_from_node(child, language))

        return references

    def _extract_identifiers_from_node(
        self, node: Node, language: cs.SupportedLanguage
    ) -> list[tuple[str, Node]]:
        identifiers: list[tuple[str, Node]] = []

        match node.type:
            case cs.TS_IDENTIFIER:
                if node.text:
                    identifiers.append((node.text.decode(cs.ENCODING_UTF8), node))
            case cs.TS_MEMBER_EXPRESSION:
                if prop := node.child_by_field_name(cs.FIELD_PROPERTY):
                    identifiers.extend(
                        self._extract_identifiers_from_node(prop, language)
                    )
            case cs.TS_ARRAY:
                for child in node.children:
                    if child.is_named:
                        identifiers.extend(
                            self._extract_identifiers_from_node(child, language)
                        )
            case cs.TS_OBJECT:
                for child in node.children:
                    if child.type == cs.TS_PAIR:
                        if value := child.child_by_field_name(cs.FIELD_VALUE):
                            identifiers.extend(
                                self._extract_identifiers_from_node(value, language)
                            )
                    elif child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN:
                        identifiers.extend(
                            self._extract_identifiers_from_node(child, language)
                        )
            case cs.TS_ATTRIBUTE if language == cs.SupportedLanguage.PYTHON:
                if attr := node.child_by_field_name(cs.FIELD_ATTRIBUTE):
                    identifiers.extend(
                        self._extract_identifiers_from_node(attr, language)
                    )

        return identifiers

    def _get_hosting_function_qn(self, call_node: Node, module_qn: str) -> str | None:
        current = call_node.parent
        while current:
            if current.type == cs.TS_VARIABLE_DECLARATOR:
                if name_node := current.child_by_field_name(cs.FIELD_NAME):
                    if name_node.text:
                        var_name = name_node.text.decode(cs.ENCODING_UTF8)
                        candidate_qn = f"{module_qn}{cs.SEPARATOR_DOT}{var_name}"
                        if candidate_qn in self._resolver.function_registry:
                            return candidate_qn
                return None
            if current.type in (cs.TS_PROGRAM, cs.TS_MODULE):
                return None
            current = current.parent
        return None

    def _extract_parameter_names(
        self, caller_node: Node, language: cs.SupportedLanguage
    ) -> frozenset[str]:
        names: set[str] = set()
        match language:
            case cs.SupportedLanguage.JS | cs.SupportedLanguage.TS:
                params_node = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
                if params_node is None:
                    for child in caller_node.children:
                        if child.type == cs.TS_FORMAL_PARAMETERS:
                            params_node = child
                            break
                if params_node is None:
                    return frozenset()
                for child in params_node.children:
                    if not child.is_named:
                        continue
                    match child.type:
                        case cs.TS_IDENTIFIER:
                            if child.text:
                                names.add(child.text.decode(cs.ENCODING_UTF8))
                        case cs.TS_REQUIRED_PARAMETER | cs.TS_OPTIONAL_PARAMETER:
                            pattern_node = child.child_by_field_name(cs.FIELD_PATTERN)
                            if (
                                pattern_node
                                and pattern_node.type == cs.TS_IDENTIFIER
                                and pattern_node.text
                            ):
                                names.add(pattern_node.text.decode(cs.ENCODING_UTF8))
                            else:
                                for subchild in child.children:
                                    if (
                                        subchild.type == cs.TS_IDENTIFIER
                                        and subchild.text
                                    ):
                                        names.add(
                                            subchild.text.decode(cs.ENCODING_UTF8)
                                        )
                                        break
            case cs.SupportedLanguage.PYTHON:
                params_node = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
                if params_node is None:
                    return frozenset()
                for child in params_node.children:
                    if not child.is_named:
                        continue
                    match child.type:
                        case cs.TS_IDENTIFIER:
                            if child.text:
                                names.add(child.text.decode(cs.ENCODING_UTF8))
                        case (
                            cs.TS_PY_TYPED_PARAMETER
                            | cs.TS_PY_TYPED_DEFAULT_PARAMETER
                            | cs.TS_PY_DEFAULT_PARAMETER
                        ):
                            name_node = child.child_by_field_name(cs.FIELD_NAME)
                            if name_node and name_node.text:
                                names.add(name_node.text.decode(cs.ENCODING_UTF8))
                            else:
                                for subchild in child.children:
                                    if (
                                        subchild.type == cs.TS_IDENTIFIER
                                        and subchild.text
                                    ):
                                        names.add(
                                            subchild.text.decode(cs.ENCODING_UTF8)
                                        )
                                        break
        return frozenset(names)

    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None = None,
    ) -> None:
        calls_query = queries[language].get(cs.QUERY_CALLS)
        if not calls_query:
            return

        try:
            local_var_types = (
                self._resolver.type_inference.build_local_variable_type_map(
                    caller_node, module_qn, language
                )
            )
        except Exception as e:
            logger.error(
                ls.CALL_TYPE_INFERENCE_FAILED.format(
                    caller=caller_qn, error=e, trace=traceback.format_exc()
                )
            )
            local_var_types = {}

        cursor = QueryCursor(calls_query)
        captures = cursor.captures(caller_node)
        call_nodes = captures.get(cs.CAPTURE_CALL, [])

        logger.debug(
            ls.CALL_FOUND_NODES.format(
                count=len(call_nodes), language=language, caller=caller_qn
            )
        )

        if caller_node.type == cs.TS_ARROW_FUNCTION and call_nodes:
            has_parenthesized_body = any(
                child.type == cs.TS_PARENTHESIZED_EXPRESSION
                for child in caller_node.children
            )
            if has_parenthesized_body:
                logger.info(
                    f"📝 Implicit return arrow function '{caller_qn}' has {len(call_nodes)} calls"
                )

        is_implicit_return_arrow = False
        if caller_node.type == cs.TS_ARROW_FUNCTION:
            has_parenthesized_body = any(
                child.type == cs.TS_PARENTHESIZED_EXPRESSION
                for child in caller_node.children
            )
            if has_parenthesized_body:
                is_implicit_return_arrow = True
                logger.debug(
                    f"Processing {len(call_nodes)} calls in implicit return arrow '{caller_qn}'"
                )

        parameter_names = self._extract_parameter_names(caller_node, language)
        import_map = self._resolver.import_processor.import_mapping.get(module_qn, {})

        for call_node in call_nodes:
            if not isinstance(call_node, Node):
                continue
            try:
                call_name = self._get_call_target_name(call_node)

                if is_implicit_return_arrow:
                    if call_name:
                        logger.debug(
                            f"Extracted call_name='{call_name}' from call at line {call_node.start_point[0] + 1}"
                        )
                    else:
                        logger.debug(
                            f"Failed to extract call_name from call at line {call_node.start_point[0] + 1}, node type={call_node.type}"
                        )

                if not call_name:
                    continue

                if call_name in parameter_names and call_name not in import_map:
                    self.metrics.calls_skipped_parameter += 1
                    continue

                self.metrics.total_call_nodes += 1

                function_refs = self._extract_function_reference_arguments(
                    call_node, language
                )

                ref_caller_type = caller_type
                ref_caller_qn = caller_qn
                if caller_type == cs.NodeLabel.MODULE:
                    if hosting_qn := self._get_hosting_function_qn(
                        call_node, module_qn
                    ):
                        ref_caller_type = cs.NodeLabel.FUNCTION
                        ref_caller_qn = hosting_qn
                        logger.debug(
                            ls.CALL_HOSTING_FUNCTION_ATTRIBUTED.format(
                                call_name=call_name, hosting_qn=hosting_qn
                            )
                        )

                for ref_name, ref_node in function_refs:
                    ref_info = self._resolver.resolve_function_call(
                        ref_name, module_qn, local_var_types, class_context
                    )

                    if ref_info:
                        ref_type, ref_qn = ref_info
                        logger.debug(f"✅ Function reference: {ref_name} → {ref_qn}")

                        self.ingestor.ensure_relationship_batch(
                            (ref_caller_type, cs.KEY_QUALIFIED_NAME, ref_caller_qn),
                            cs.RelationshipType.CALLS,
                            (ref_type, cs.KEY_QUALIFIED_NAME, ref_qn),
                        )
                    else:
                        logger.debug(f"Unresolved reference: {ref_name}")

                if (
                    language == cs.SupportedLanguage.JAVA
                    and call_node.type == cs.TS_METHOD_INVOCATION
                ):
                    callee_info = self._resolver.resolve_java_method_call(
                        call_node, module_qn, local_var_types
                    )
                else:
                    callee_info = self._resolver.resolve_function_call(
                        call_name, module_qn, local_var_types, class_context
                    )
                if callee_info:
                    callee_type, callee_qn = callee_info
                    resolution_strategy = cs.RESOLUTION_STRATEGY_FUNCTION_CALL
                elif builtin_info := self._resolver.resolve_builtin_call(call_name):
                    callee_type, callee_qn = builtin_info
                    resolution_strategy = cs.RESOLUTION_STRATEGY_BUILTIN_CALL
                elif operator_info := self._resolver.resolve_cpp_operator_call(
                    call_name, module_qn
                ):
                    callee_type, callee_qn = operator_info
                    resolution_strategy = cs.RESOLUTION_STRATEGY_CPP_OPERATOR
                else:
                    self.metrics.calls_unresolved += 1
                    if caller_node.type == cs.TS_ARROW_FUNCTION:
                        has_parenthesized_body = any(
                            child.type == cs.TS_PARENTHESIZED_EXPRESSION
                            for child in caller_node.children
                        )
                        if has_parenthesized_body:
                            logger.debug(
                                f"Failed to resolve call '{call_name}' from implicit return arrow '{caller_qn}'"
                            )
                    has_import_map = (
                        module_qn in self._resolver.import_processor.import_mapping
                    )
                    import_map_size = len(
                        self._resolver.import_processor.import_mapping.get(
                            module_qn, {}
                        )
                    )
                    logger.debug(
                        ls.CALL_UNRESOLVED_DETAILS.format(
                            call_name=call_name,
                            caller_qn=caller_qn,
                            module_qn=module_qn,
                            has_import_map=has_import_map,
                            import_map_size=import_map_size,
                        )
                    )
                    continue
                self.metrics.calls_resolved += 1
                self.metrics.resolution_by_strategy[resolution_strategy] = (
                    self.metrics.resolution_by_strategy.get(resolution_strategy, 0) + 1
                )
                logger.debug(
                    ls.CALL_FOUND.format(
                        caller=caller_qn,
                        call_name=call_name,
                        callee_type=callee_type,
                        callee_qn=callee_qn,
                    )
                )

                self.ingestor.ensure_relationship_batch(
                    (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn),
                    cs.RelationshipType.CALLS,
                    (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                )
            except Exception as e:
                self.metrics.calls_errored += 1
                logger.error(
                    ls.CALL_INGEST_NODE_FAILED.format(
                        line=call_node.start_point[0] + 1,
                        caller=caller_qn,
                        error=e,
                        trace=traceback.format_exc(),
                    )
                )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts: list[str] = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT.format(
                    node=func_node, parent_type=type(current)
                )
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if parent_name := self._get_node_name(current):
                    path_parts.append(parent_name)
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        return is_method_node(func_node, lang_config)
