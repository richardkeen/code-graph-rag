import sys
from collections import OrderedDict, defaultdict
from collections.abc import Callable, ItemsView, KeysView
from pathlib import Path

from loguru import logger
from tree_sitter import Node, Parser

from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
from .config import settings
from .language_spec import LANGUAGE_FQN_SPECS, get_language_spec
from .models import ScanFunnelMetrics
from .parsers.factory import ProcessorFactory
from .services import IngestorProtocol, QueryProtocol
from .types_defs import (
    EmbeddingQueryResult,
    FunctionRegistry,
    LanguageQueries,
    NodeType,
    QualifiedName,
    ResultRow,
    SimpleNameLookup,
    TrieNode,
)
from .utils.dependencies import has_semantic_dependencies
from .utils.fqn_resolver import find_function_source_by_fqn
from .utils.path_utils import discover_repo_files, should_skip_path
from .utils.source_extraction import extract_source_with_fallback


class FunctionRegistryTrie:
    def __init__(self, simple_name_lookup: SimpleNameLookup | None = None) -> None:
        self.root: TrieNode = {}
        self._entries: FunctionRegistry = {}
        self._simple_name_lookup = simple_name_lookup

    def insert(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self._entries[qualified_name] = func_type

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        current: TrieNode = self.root

        for part in parts:
            if part not in current:
                current[part] = {}
            child = current[part]
            assert isinstance(child, dict)
            current = child

        current[cs.TRIE_TYPE_KEY] = func_type
        current[cs.TRIE_QN_KEY] = qualified_name

    def get(
        self, qualified_name: QualifiedName, default: NodeType | None = None
    ) -> NodeType | None:
        return self._entries.get(qualified_name, default)

    def __contains__(self, qualified_name: QualifiedName) -> bool:
        return qualified_name in self._entries

    def __getitem__(self, qualified_name: QualifiedName) -> NodeType:
        return self._entries[qualified_name]

    def __setitem__(self, qualified_name: QualifiedName, func_type: NodeType) -> None:
        self.insert(qualified_name, func_type)

    def __delitem__(self, qualified_name: QualifiedName) -> None:
        if qualified_name not in self._entries:
            return

        del self._entries[qualified_name]

        parts = qualified_name.split(cs.SEPARATOR_DOT)
        self._cleanup_trie_path(parts, self.root)

    def _cleanup_trie_path(self, parts: list[str], node: TrieNode) -> bool:
        if not parts:
            node.pop(cs.TRIE_QN_KEY, None)
            node.pop(cs.TRIE_TYPE_KEY, None)
            return not node

        part = parts[0]
        if part not in node:
            return False

        child = node[part]
        assert isinstance(child, dict)
        if self._cleanup_trie_path(parts[1:], child):
            del node[part]

        is_endpoint = cs.TRIE_QN_KEY in node
        has_children = any(not key.startswith(cs.TRIE_INTERNAL_PREFIX) for key in node)
        return not has_children and not is_endpoint

    def _navigate_to_prefix(self, prefix: str) -> TrieNode | None:
        parts = prefix.split(cs.SEPARATOR_DOT) if prefix else []
        current: TrieNode = self.root
        for part in parts:
            if part not in current:
                return None
            child = current[part]
            assert isinstance(child, dict)
            current = child
        return current

    def _collect_from_subtree(
        self,
        node: TrieNode,
        filter_fn: Callable[[QualifiedName], bool] | None = None,
    ) -> list[tuple[QualifiedName, NodeType]]:
        results: list[tuple[QualifiedName, NodeType]] = []

        def dfs(n: TrieNode) -> None:
            if cs.TRIE_QN_KEY in n:
                qn = n[cs.TRIE_QN_KEY]
                func_type = n[cs.TRIE_TYPE_KEY]
                assert isinstance(qn, str) and isinstance(func_type, NodeType)
                if filter_fn is None or filter_fn(qn):
                    results.append((qn, func_type))

            for key, child in n.items():
                if not key.startswith(cs.TRIE_INTERNAL_PREFIX):
                    assert isinstance(child, dict)
                    dfs(child)

        dfs(node)
        return results

    def keys(self) -> KeysView[QualifiedName]:
        return self._entries.keys()

    def items(self) -> ItemsView[QualifiedName, NodeType]:
        return self._entries.items()

    def __len__(self) -> int:
        return len(self._entries)

    def find_with_prefix_and_suffix(
        self, prefix: str, suffix: str
    ) -> list[QualifiedName]:
        node = self._navigate_to_prefix(prefix)
        if node is None:
            return []
        suffix_pattern = f".{suffix}"
        matches = self._collect_from_subtree(
            node, lambda qn: qn.endswith(suffix_pattern)
        )
        return [qn for qn, _ in matches]

    def find_ending_with(self, suffix: str) -> list[QualifiedName]:
        if self._simple_name_lookup is not None and suffix in self._simple_name_lookup:
            return list(self._simple_name_lookup[suffix])
        return [qn for qn in self._entries.keys() if qn.endswith(f".{suffix}")]

    def register_external(
        self, qualified_name: QualifiedName, func_type: NodeType
    ) -> None:
        self._entries[qualified_name] = func_type

    def find_with_prefix(self, prefix: str) -> list[tuple[QualifiedName, NodeType]]:
        node = self._navigate_to_prefix(prefix)
        return [] if node is None else self._collect_from_subtree(node)


class BoundedASTCache:
    def __init__(
        self,
        max_entries: int | None = None,
        max_memory_mb: int | None = None,
    ):
        self.cache: OrderedDict[Path, tuple[Node, cs.SupportedLanguage]] = OrderedDict()
        self.max_entries = (
            max_entries if max_entries is not None else settings.CACHE_MAX_ENTRIES
        )
        max_mem = (
            max_memory_mb if max_memory_mb is not None else settings.CACHE_MAX_MEMORY_MB
        )
        self.max_memory_bytes = max_mem * cs.BYTES_PER_MB

    def __setitem__(self, key: Path, value: tuple[Node, cs.SupportedLanguage]) -> None:
        if key in self.cache:
            del self.cache[key]

        self.cache[key] = value

        self._enforce_limits()

    def __getitem__(self, key: Path) -> tuple[Node, cs.SupportedLanguage]:
        value = self.cache[key]
        self.cache.move_to_end(key)
        return value

    def __delitem__(self, key: Path) -> None:
        if key in self.cache:
            del self.cache[key]

    def __contains__(self, key: Path) -> bool:
        return key in self.cache

    def items(self) -> ItemsView[Path, tuple[Node, cs.SupportedLanguage]]:
        return self.cache.items()

    def _enforce_limits(self) -> None:
        while len(self.cache) > self.max_entries:
            self.cache.popitem(last=False)

        if self._should_evict_for_memory():
            entries_to_remove = max(
                1, len(self.cache) // settings.CACHE_EVICTION_DIVISOR
            )
            for _ in range(entries_to_remove):
                if self.cache:
                    self.cache.popitem(last=False)

    def _should_evict_for_memory(self) -> bool:
        try:
            cache_size = sum(sys.getsizeof(v) for v in self.cache.values())
            return cache_size > self.max_memory_bytes
        except Exception:
            return (
                len(self.cache)
                > self.max_entries * settings.CACHE_MEMORY_THRESHOLD_RATIO
            )


class GraphUpdater:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        parsers: dict[cs.SupportedLanguage, Parser],
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        unignore_paths: frozenset[str] | None = None,
        exclude_paths: frozenset[str] | None = None,
        file_filter: list[Path] | None = None,
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.parsers = parsers
        self.queries = queries
        self.project_name = repo_path.resolve().name
        self.simple_name_lookup: SimpleNameLookup = defaultdict(set)
        self.function_registry = FunctionRegistryTrie(
            simple_name_lookup=self.simple_name_lookup
        )
        self.ast_cache = BoundedASTCache()
        self.unignore_paths = unignore_paths
        self.exclude_paths = exclude_paths
        self.file_filter = file_filter

        from .parsers.workspace.factory import WorkspaceResolverFactory

        self.workspace_resolver_factory = WorkspaceResolverFactory(
            repo_path=self.repo_path,
            project_name=self.project_name,
        )
        self.workspace_resolver = self.workspace_resolver_factory.create_resolver()

        self.factory = ProcessorFactory(
            ingestor=self.ingestor,
            repo_path=self.repo_path,
            project_name=self.project_name,
            workspace_resolver=self.workspace_resolver,
            queries=self.queries,
            function_registry=self.function_registry,
            simple_name_lookup=self.simple_name_lookup,
            ast_cache=self.ast_cache,
            unignore_paths=self.unignore_paths,
            exclude_paths=self.exclude_paths,
        )

    def _is_dependency_file(self, file_name: str, filepath: Path) -> bool:
        return (
            file_name.lower() in cs.DEPENDENCY_FILES
            or filepath.suffix.lower() == cs.CSPROJ_SUFFIX
        )

    def _load_function_registry_from_graph(self) -> None:
        if not isinstance(self.ingestor, QueryProtocol):
            logger.warning(ls.SCAN_REGISTRY_PRELOAD_NO_QUERY)
            return
        results = self.ingestor.fetch_all(cq.CYPHER_LOAD_FUNCTION_REGISTRY)
        for row in results:
            qn = row.get("qn")
            raw_type = row.get("type")
            if isinstance(qn, str) and isinstance(raw_type, str):
                try:
                    node_type = NodeType(raw_type)
                except ValueError:
                    continue
                self.function_registry.insert(qn, node_type)
                simple_name = qn.rsplit(".", 1)[-1]
                self.simple_name_lookup[simple_name].add(qn)
        logger.info(ls.SCAN_REGISTRY_PRELOADED.format(count=len(results)))

    def run(self) -> None:
        try:
            self.ingestor.ensure_node_batch(
                cs.NODE_PROJECT, {cs.KEY_NAME: self.project_name}
            )
            logger.info(ls.ENSURING_PROJECT.format(name=self.project_name))

            if self.file_filter:
                logger.info(
                    ls.SCAN_FILE_FILTER_ACTIVE.format(count=len(self.file_filter))
                )
                self._load_function_registry_from_graph()
            else:
                logger.info(ls.PASS_1_STRUCTURE)
                self.factory.structure_processor.identify_structure()

            logger.info(ls.PASS_2_FILES)
            self._process_files()

            logger.info(ls.FOUND_FUNCTIONS.format(count=len(self.function_registry)))
            logger.info(ls.PASS_3_CALLS)
            self._process_function_calls()
            self._log_call_processing_summary()

            self.factory.definition_processor.process_all_method_overrides()

            logger.info(ls.ANALYSIS_COMPLETE)
            self.ingestor.flush_all()

            self._generate_semantic_embeddings()
        finally:
            self._cleanup_subprocesses()

    def _cleanup_subprocesses(self) -> None:
        if self.factory._type_resolver:
            self.factory._type_resolver.cleanup()
        if hasattr(self.factory._module_resolver, "cleanup"):
            self.factory._module_resolver.cleanup()

    def remove_file_from_state(self, file_path: Path) -> None:
        logger.debug(ls.REMOVING_STATE.format(path=file_path))

        if file_path in self.ast_cache:
            del self.ast_cache[file_path]
            logger.debug(ls.REMOVED_FROM_CACHE)

        relative_path = file_path.relative_to(self.repo_path)
        path_parts = (
            relative_path.parent.parts
            if file_path.name == cs.INIT_PY
            else relative_path.with_suffix("").parts
        )
        module_qn_prefix = cs.SEPARATOR_DOT.join([self.project_name, *path_parts])

        qns_to_remove = set()

        for qn in list(self.function_registry.keys()):
            if qn.startswith(f"{module_qn_prefix}.") or qn == module_qn_prefix:
                qns_to_remove.add(qn)
                del self.function_registry[qn]

        if qns_to_remove:
            logger.debug(ls.REMOVING_QNS.format(count=len(qns_to_remove)))

        for simple_name, qn_set in self.simple_name_lookup.items():
            original_count = len(qn_set)
            new_qn_set = qn_set - qns_to_remove
            if len(new_qn_set) < original_count:
                self.simple_name_lookup[simple_name] = new_qn_set
                logger.debug(ls.CLEANED_SIMPLE_NAME.format(name=simple_name))

    def _process_files(self) -> None:
        scan_metrics = ScanFunnelMetrics()

        if self.file_filter:
            all_files: list[Path] = list(self.file_filter)
        else:
            all_files = discover_repo_files(self.repo_path)

        scan_metrics.files_discovered = len(all_files)

        for filepath in all_files:
            if not filepath.is_file():
                continue
            if should_skip_path(
                filepath,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            ):
                scan_metrics.files_filtered_exclude += 1
                continue

            lang_config = get_language_spec(filepath.suffix)
            if (
                lang_config
                and isinstance(lang_config.language, cs.SupportedLanguage)
                and lang_config.language in self.parsers
            ):
                try:
                    result = self.factory.definition_processor.process_file(
                        filepath,
                        lang_config.language,
                        self.queries,
                        self.factory.structure_processor.structural_elements,
                    )
                    if result:
                        root_node, language = result
                        self.ast_cache[filepath] = (root_node, language)
                    scan_metrics.files_parsed_as_code += 1
                except Exception as e:
                    scan_metrics.files_parse_failed += 1
                    logger.warning(ls.SCAN_PARSE_FAILED.format(path=filepath, error=e))
            elif self._is_dependency_file(filepath.name, filepath):
                self.factory.definition_processor.process_dependencies(filepath)
                scan_metrics.files_parsed_as_dependency += 1
            else:
                scan_metrics.files_filtered_no_parser += 1
                ext = filepath.suffix
                scan_metrics.extensions_skipped[ext] = (
                    scan_metrics.extensions_skipped.get(ext, 0) + 1
                )

            self.factory.structure_processor.process_generic_file(
                filepath, filepath.name
            )

        self.scan_metrics = scan_metrics
        logger.info(
            ls.SCAN_FUNNEL_SUMMARY.format(
                discovered=scan_metrics.files_discovered,
                code=scan_metrics.files_parsed_as_code,
                dependency=scan_metrics.files_parsed_as_dependency,
                exclude=scan_metrics.files_filtered_exclude,
                no_parser=scan_metrics.files_filtered_no_parser,
                failed=scan_metrics.files_parse_failed,
            )
        )

    def _log_call_processing_summary(self) -> None:
        m = self.factory.call_processor.metrics
        logger.info(
            ls.CALL_METRICS_SUMMARY.format(
                files=m.files_attempted, nodes=m.total_call_nodes
            )
        )
        logger.info(
            ls.CALL_METRICS_RESOLUTION.format(
                resolved=m.calls_resolved,
                unresolved=m.calls_unresolved,
                errored=m.calls_errored,
            )
        )
        logger.info(ls.CALL_SKIPPED_PARAMETER.format(count=m.calls_skipped_parameter))
        if m.resolution_by_strategy:
            logger.info(
                ls.CALL_METRICS_STRATEGIES.format(
                    strategies=dict(m.resolution_by_strategy)
                )
            )
        logger.info(
            ls.CALL_METRICS_ZERO_CALLS.format(count=len(m.files_with_zero_calls))
        )
        logger.info(
            ls.CALL_METRICS_ERROR_RATE.format(
                files_with_errors=m.files_with_errors, files_attempted=m.files_attempted
            )
        )

    def _process_function_calls(self) -> None:
        ast_cache_items = list(self.ast_cache.items())
        for file_path, (root_node, language) in ast_cache_items:
            self.factory.call_processor.process_calls_in_file(
                file_path, root_node, language, self.queries
            )

    def _generate_semantic_embeddings(self) -> None:
        if not has_semantic_dependencies():
            logger.info(ls.SEMANTIC_NOT_AVAILABLE)
            return

        if not isinstance(self.ingestor, QueryProtocol):
            logger.info(ls.INGESTOR_NO_QUERY)
            return

        try:
            from .embedder import embed_code
            from .vector_store import store_embedding

            logger.info(ls.PASS_4_EMBEDDINGS)

            results = self.ingestor.fetch_all(
                cs.CYPHER_QUERY_EMBEDDINGS, {"project_name": self.project_name + "."}
            )

            if not results:
                logger.info(ls.NO_FUNCTIONS_FOR_EMBEDDING)
                return

            logger.info(ls.GENERATING_EMBEDDINGS.format(count=len(results)))

            embedded_count = 0
            for row in results:
                parsed = self._parse_embedding_result(row)
                if parsed is None:
                    continue

                node_id = parsed[cs.KEY_NODE_ID]
                qualified_name = parsed[cs.KEY_QUALIFIED_NAME]
                start_line = parsed.get(cs.KEY_START_LINE)
                end_line = parsed.get(cs.KEY_END_LINE)
                file_path = parsed.get(cs.KEY_PATH)

                if start_line is None or end_line is None or file_path is None:
                    logger.debug(ls.NO_SOURCE_FOR.format(name=qualified_name))

                elif source_code := self._extract_source_code(
                    qualified_name, file_path, start_line, end_line
                ):
                    try:
                        embedding = embed_code(source_code)
                        store_embedding(node_id, embedding, qualified_name)
                        embedded_count += 1

                        if embedded_count % settings.EMBEDDING_PROGRESS_INTERVAL == 0:
                            logger.debug(
                                ls.EMBEDDING_PROGRESS.format(
                                    done=embedded_count, total=len(results)
                                )
                            )

                    except Exception as e:
                        logger.warning(
                            ls.EMBEDDING_FAILED.format(name=qualified_name, error=e)
                        )
                else:
                    logger.debug(ls.NO_SOURCE_FOR.format(name=qualified_name))
            logger.info(ls.EMBEDDINGS_COMPLETE.format(count=embedded_count))

        except Exception as e:
            logger.warning(ls.EMBEDDING_GENERATION_FAILED.format(error=e))

    def _extract_source_code(
        self, qualified_name: str, file_path: str, start_line: int, end_line: int
    ) -> str | None:
        if not file_path or not start_line or not end_line:
            return None

        file_path_obj = self.repo_path / file_path

        ast_extractor = None
        if file_path_obj in self.ast_cache:
            root_node, language = self.ast_cache[file_path_obj]
            fqn_config = LANGUAGE_FQN_SPECS.get(language)

            if fqn_config:

                def ast_extractor_func(qname: str, path: Path) -> str | None:
                    return find_function_source_by_fqn(
                        root_node,
                        qname,
                        path,
                        self.repo_path,
                        self.project_name,
                        fqn_config,
                    )

                ast_extractor = ast_extractor_func

        return extract_source_with_fallback(
            file_path_obj, start_line, end_line, qualified_name, ast_extractor
        )

    def _parse_embedding_result(self, row: ResultRow) -> EmbeddingQueryResult | None:
        node_id = row.get(cs.KEY_NODE_ID)
        qualified_name = row.get(cs.KEY_QUALIFIED_NAME)

        if not isinstance(node_id, int) or not isinstance(qualified_name, str):
            return None

        start_line = row.get(cs.KEY_START_LINE)
        end_line = row.get(cs.KEY_END_LINE)
        file_path = row.get(cs.KEY_PATH)

        return EmbeddingQueryResult(
            node_id=node_id,
            qualified_name=qualified_name,
            start_line=start_line if isinstance(start_line, int) else None,
            end_line=end_line if isinstance(end_line, int) else None,
            path=file_path if isinstance(file_path, str) else None,
        )
