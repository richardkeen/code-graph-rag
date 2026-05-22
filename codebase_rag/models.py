from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from rich.console import Console

from .constants import SupportedLanguage
from .types_defs import MCPHandlerType, MCPInputSchema, PropertyValue

if TYPE_CHECKING:
    from tree_sitter import Node


@dataclass
class SessionState:
    confirm_edits: bool = True
    log_file: Path | None = None
    cancelled: bool = False

    def reset_cancelled(self) -> None:
        self.cancelled = False


def _default_console() -> Console:
    return Console(width=None, force_terminal=True)


@dataclass
class AppContext:
    session: SessionState = field(default_factory=SessionState)
    console: Console = field(default_factory=_default_console)


@dataclass
class GraphNode:
    node_id: int
    labels: list[str]
    properties: dict[str, PropertyValue]


@dataclass
class GraphRelationship:
    from_id: int
    to_id: int
    type: str
    properties: dict[str, PropertyValue]


class FQNSpec(NamedTuple):
    scope_node_types: frozenset[str]
    function_node_types: frozenset[str]
    get_name: Callable[["Node"], str | None]
    file_to_module_parts: Callable[[Path, Path], list[str]]


@dataclass(frozen=True)
class LanguageSpec:
    language: SupportedLanguage | str
    file_extensions: tuple[str, ...]
    function_node_types: tuple[str, ...]
    class_node_types: tuple[str, ...]
    module_node_types: tuple[str, ...]
    call_node_types: tuple[str, ...] = ()
    import_node_types: tuple[str, ...] = ()
    import_from_node_types: tuple[str, ...] = ()
    name_field: str = "name"
    body_field: str = "body"
    package_indicators: tuple[str, ...] = ()
    # Extensions whose presence in the repo proves this language has real
    # sources, for the purpose of activating `package_indicators`. Defaults
    # to file_extensions. Override when an extension is shared with build
    # scripts that exist in non-source-bearing repos (e.g. Kotlin uses
    # (".kt",) so `build.gradle.kts` alone in a Java repo doesn't activate
    # Kotlin's package indicators).
    package_source_extensions: tuple[str, ...] | None = None
    function_query: str | None = None
    class_query: str | None = None
    call_query: str | None = None


@dataclass
class Dependency:
    name: str
    spec: str
    category: str = field(default="", compare=False)
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class MethodModifiersAndAnnotations:
    modifiers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)


@dataclass
class ToolMetadata:
    name: str
    description: str
    input_schema: MCPInputSchema
    handler: MCPHandlerType
    returns_json: bool


@dataclass
class CallProcessingMetrics:
    files_attempted: int = 0
    files_with_errors: int = 0
    total_call_nodes: int = 0
    calls_resolved: int = 0
    calls_unresolved: int = 0
    calls_errored: int = 0
    calls_skipped_parameter: int = 0
    calls_resolved_external: int = 0
    external_functions_created: int = 0
    resolution_by_strategy: dict[str, int] = field(default_factory=dict)
    files_with_zero_calls: list[str] = field(default_factory=list)


@dataclass
class TypeResolutionMetrics:
    files_queried: int = 0
    functions_with_types: int = 0
    parameters_resolved: int = 0
    types_filtered_complex: int = 0
    files_not_in_program: int = 0


@dataclass
class ScanFunnelMetrics:
    files_discovered: int = 0
    files_filtered_exclude: int = 0
    files_filtered_no_parser: int = 0
    files_parsed_as_code: int = 0
    files_parsed_as_dependency: int = 0
    files_parse_failed: int = 0
    extensions_skipped: dict[str, int] = field(default_factory=dict)
    index_files_normalized: int = 0
