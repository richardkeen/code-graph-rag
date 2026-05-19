# Add and verify Kotlin language support

## Context

`code-graph-rag` parses source code with tree-sitter, builds a knowledge graph, and answers natural-language questions about it. Goal: ship Kotlin parity with the Java handler — top-level functions, classes, imports, calls, plus Kotlin-specific semantics (companion objects, extension functions, data/sealed/value classes, primary/secondary constructors).

Branch: `feat/kotlin-language-support`. Grammar: **`tree-sitter-grammars/tree-sitter-kotlin`** consumed via the PyPI package `tree-sitter-kotlin`. E2E target: **ktorio/ktor-samples**.

## Grammar choice

Use **`tree-sitter-kotlin` from PyPI** (the [`tree-sitter-grammars/tree-sitter-kotlin`](https://github.com/tree-sitter-grammars/tree-sitter-kotlin) project, currently v1.1.0). Reasons:

- It is the only Kotlin tree-sitter grammar that ships maintained Python bindings on PyPI.
- It exposes named fields on every declaration we care about (`name` on `class_declaration` / `object_declaration` / `companion_object` / `function_declaration` / `type_alias`, `type` on `type_alias`), which lets the generic FQN resolver do the right thing without per-language positional walks.
- Slotting it into `treesitter-full` keeps Kotlin consistent with how every other language is consumed — no submodules, no on-the-fly builds.

Beware that `tree-sitter/kotlin-tree-sitter` is *not* a grammar; it's a Kotlin client library for *using* tree-sitter from Kotlin. `tree-sitter/tree-sitter-kotlin` does not exist (404). Don't reach for either.

## Architecture pointers

- `codebase_rag/constants.py` — `SupportedLanguage`, `LanguageMetadata`, per-language `SPEC_*_*` and `FQN_*_*` tuples, `TS_*_*` node names
- `codebase_rag/language_spec.py` — `LANGUAGE_SPECS`, `LANGUAGE_FQN_SPECS`, language-specific name resolvers (e.g. `_kotlin_get_name`, `_rust_get_name`)
- `codebase_rag/parser_loader.py` — `_import_language_loaders` registers PyPI bindings via `LanguageImport` entries
- `codebase_rag/parsers/handlers/` — tiny per-language classes overriding `extract_decorators` + `build_method_qualified_name` + `find_class_body` (see `handlers/java.py`, 28 lines)
- `codebase_rag/parsers/handlers/registry.py` — `_HANDLERS` dict
- `codebase_rag/parsers/<lang>/utils.py` — per-language extraction utilities (see `parsers/java/utils.py`)
- `codebase_rag/parsers/class_ingest/mixin.py` — generic class/method ingest pipeline. **Critical line: `mixin.py:_ingest_class_methods` calls `class_node.child_by_field_name("body")`**, which returns `None` for `tree-sitter-kotlin` (no `body` field). Resolved via the `find_class_body` extension point on `LanguageHandler` (default = the `body` field; Kotlin overrides to scan children for `class_body` / `enum_class_body`).
- Tests: `codebase_rag/tests/test_java_*.py` (22 files) for the canonical pattern; `tests/conftest.py` for `temp_repo`, `mock_ingestor`, `create_and_run_updater`, `get_node_names`

## Phase A — plumbing

The infrastructural wiring that lets `cgr` know Kotlin exists, without semantics.

- `pyproject.toml`: add `tree-sitter-kotlin>=1.1.0` to the `treesitter-full` extras.
- `codebase_rag/constants.py`: `EXT_KT`/`EXT_KTS`/`KOTLIN_EXTENSIONS`, `SupportedLanguage.KOTLIN`, `TreeSitterModule.KOTLIN`, `LANGUAGE_METADATA[KOTLIN]`, all `TS_KOTLIN_*` node-name constants, `SPEC_KOTLIN_FUNCTION_TYPES`/`CLASS_TYPES`/`MODULE_TYPES`/`CALL_TYPES`/`IMPORT_TYPES`, `FQN_KOTLIN_SCOPE_TYPES`/`FUNCTION_TYPES`, `KOTLIN_COMPANION_DEFAULT_NAME`. Note: `TS_KOTLIN_NAVIGATION_EXPRESSION` is deliberately **excluded** from `SPEC_KOTLIN_CALL_TYPES` because at the top level it fires for every property access, not just calls — the structured form belongs in Phase B's `call_query`.
- `codebase_rag/language_spec.py`: `_kotlin_get_name`, `KOTLIN_FQN_SPEC` registered in `LANGUAGE_FQN_SPECS`, `LANGUAGE_SPECS[KOTLIN]` entry.
- `codebase_rag/parser_loader.py`: `LanguageImport(KOTLIN, TreeSitterModule.KOTLIN, QUERY_LANGUAGE, KOTLIN)`.
- `codebase_rag/tests/test_kotlin_smoke.py`: asserts a 1-class 1-function `.kt` fixture produces named Module + Class + top-level Function.

## Phase B — semantic correctness

The parts that need real semantics: a body-lookup hook, hand-written queries, and a small handler.

### B1. `codebase_rag/parsers/class_ingest/mixin.py` — add a `body` resolver hook

This is the highest-priority change because without it, no Kotlin methods reach the graph as `Method` nodes.

`_ingest_class_methods` calls `class_node.child_by_field_name("body")` — but `tree-sitter-kotlin`'s `class_declaration` doesn't expose a `body` field. Add a small indirection:

- Define a protocol method on `LanguageHandler` (`parsers/handlers/protocol.py`): `find_class_body(class_node) -> ASTNode | None`. Default implementation: `return class_node.child_by_field_name("body")`.
- `BaseLanguageHandler` keeps the default behavior — every existing language is unaffected.
- `KotlinHandler` (B4) overrides it to scan children for type `class_body` (or `enum_class_body` for enum classes).
- `_ingest_class_methods` and `_ingest_rust_impl_methods` call `handler.find_class_body(class_node)` instead of the field-name shortcut.

This mirrors the existing extension-point pattern for `extract_decorators` and `build_method_qualified_name`.

### B1a. `codebase_rag/parsers/class_ingest/mixin.py` — filter nested-class methods

Tree-sitter queries are unanchored by default, so running the function query against an outer class's body also captures functions declared inside nested companions / objects / classes. Without a filter, the same function ends up ingested twice — once for the outer class (with the wrong FQN) and once for the nested class (with the right one).

Add `_is_direct_class_member(method_node, class_node)` — walks `method_node.parent` upward and returns True iff the *nearest* class-like ancestor is the class node currently being processed. Compare ancestors by tree-sitter `node.id`, since the Python bindings wrap each `.parent` lookup in a fresh object (so `is` and `==` are unreliable). Apply only when `language == KOTLIN` to keep blast radius small.

### B2. `codebase_rag/language_spec.py` — hand-written queries on `LANGUAGE_SPECS[KOTLIN]`

The auto-detected node-type lists generate naive `(node_type) @capture` queries; the structured queries below also extract names and disambiguate calls from member access. Validate node names against the grammar's `node-types.json` before committing.

```text
function_query:
  (function_declaration name: (identifier) @name) @function
  (primary_constructor) @function
  (secondary_constructor) @function
  (anonymous_function) @function
  (getter) @function
  (setter) @function

class_query:
  (class_declaration  name: (identifier) @name) @class
  (object_declaration name: (identifier) @name) @class
  (companion_object) @class
  (type_alias         type: (identifier) @name) @class

call_query:
  (call_expression (identifier) @name) @call
  (call_expression
      (navigation_expression (identifier) @name)) @call
  (infix_expression (identifier) @name) @call
```

The `call_query` version of navigation calls is the structured re-introduction of `navigation_expression` that Phase A excluded from `SPEC_KOTLIN_CALL_TYPES`.

### B3. `codebase_rag/parsers/kotlin/__init__.py` + `parsers/kotlin/utils.py` — new package

Mirror `parsers/java/utils.py`. Functions to expose:

- `extract_package_name(root_node) -> str | None` — read `package_header`, join its `qualified_identifier` child's `identifier` parts with `.`.
- `extract_imports(root_node) -> list[KotlinImport]` — handle plain `import a.b.C`, wildcards (`import a.b.*` — trailing `.` and `*` literal children), and aliased imports (`import a.b.C as D` — trailing `as` keyword and an `identifier`).
- `extract_class_modifiers(class_node) -> list[str]` — scan the `modifiers` child (if present) for `class_modifier` children, returning `data | sealed | value | open | abstract | inner | enum | annotation`.
- `extract_function_info(fn_node) -> KotlinFunctionInfo` — name, parameter type list (from `function_value_parameters` → `parameter` children → their `type`/`user_type` → `identifier`; or from `class_parameters` → `class_parameter` for primary constructors), receiver type for extension functions (the `user_type` child appearing **before** the `name` field), modifiers (`suspend`, `inline`, `infix`, `operator`).
- `is_companion_object(node) -> bool`.
- `find_class_body(class_node) -> ASTNode | None` — iterate children, return the first whose type is `class_body` or `enum_class_body`. Used by `KotlinHandler` (B4).
- `parse_import_header(import_node) -> KotlinImport | None` — used by `import_processor` for one captured `import` node.

Annotations sit under `modifiers` as `annotation` nodes whose children include either a `user_type` (e.g. `@Service`) or a `constructor_invocation` (e.g. `@JvmName("x")`); a small annotation extractor reads the type name out of either shape.

### B4. `codebase_rag/parsers/handlers/kotlin.py` — new handler (~30 lines)

Mirror `handlers/java.py`. Methods:

- `extract_decorators(node) -> list[str]` — delegate to `kotlin_utils.extract_annotations`.
- `build_method_qualified_name(class_qn, method_name, method_node) -> str` — build `class_qn.method_name(Type1, Type2)` from `extract_function_info`'s parameter type list. The mixin filter (B1a) guarantees `class_qn` is already the immediately enclosing class's qualified name (e.g. `OuterClass.Companion`), so no special routing for companions is required — the default `f"{class_qn}.{method_name}"` produces the right shape.
- `find_class_body(class_node) -> ASTNode | None` — delegates to `kotlin_utils.find_class_body`. This is the override that satisfies B1's protocol.

For top-level extension functions like `fun String.shout()`, the receiver type is folded into the FQN inside `_kotlin_get_name` (in `language_spec.py`) so the resolver produces `module.String.shout` rather than a free function. Methods declared inside classes don't need this — `class_qn` already qualifies them.

### B5. `codebase_rag/parsers/handlers/registry.py` — register

```python
from .kotlin import KotlinHandler
...
SupportedLanguage.KOTLIN: KotlinHandler,
```

### B6. Constants additions (small)

Most landed in Phase A. Phase B adds (in `constants.py`):

- `TS_KOTLIN_CLASS_BODY = "class_body"`, `TS_KOTLIN_ENUM_CLASS_BODY = "enum_class_body"` (used by `find_class_body`).
- `TS_KOTLIN_MODIFIERS`, `TS_KOTLIN_ANNOTATION`, `TS_KOTLIN_USER_TYPE`, `TS_KOTLIN_IDENTIFIER`, `TS_KOTLIN_QUALIFIED_IDENTIFIER`, `TS_KOTLIN_TYPE`, `TS_KOTLIN_NULLABLE_TYPE` (used in extraction utilities).
- `TS_KOTLIN_FUNCTION_VALUE_PARAMETERS`, `TS_KOTLIN_CLASS_PARAMETER`, `TS_KOTLIN_CLASS_PARAMETERS`, `TS_KOTLIN_PARAMETER`.
- `TS_KOTLIN_DELEGATION_SPECIFIER`, `TS_KOTLIN_DELEGATION_SPECIFIERS`, `TS_KOTLIN_CONSTRUCTOR_INVOCATION`, `TS_KOTLIN_EXPLICIT_DELEGATION` (for inheritance extraction).
- `TS_KOTLIN_VALUE_ARGUMENTS`, `TS_KOTLIN_VALUE_ARGUMENT`, `TS_KOTLIN_NUMBER_LITERAL`.
- Modifier name constants used by the handler: `KOTLIN_MODIFIER_DATA`, `..._SEALED`, `..._VALUE`, `..._OPEN`, `..._ABSTRACT`, `..._SUSPEND`, `..._INLINE`, `..._INFIX`, `..._OPERATOR`, etc.

### B7. `codebase_rag/parsers/call_processor.py` — Kotlin call-target name extraction

`_get_call_target_name` looks up `child_by_field_name("function")` and `child_by_field_name("name")`. Kotlin's `call_expression` and `infix_expression` have no fields — they use positional children — so without a Kotlin branch every Kotlin call resolves to `None` and no `CALLS` edges are emitted.

Add `_get_kotlin_call_target_name`:
- For `call_expression`: first named child is either `(identifier)` (direct call like `User(1)`) or `(navigation_expression …)` (method call like `a.b.c()`); in the latter case the trailing `identifier` child of the `navigation_expression` is the method name.
- For `infix_expression`: middle named child is the operator function (e.g. `1 to 2` → `to`).

Gate the Kotlin path on absence of both `function` and `name` fields, since the JS/TS `call_expression` shares the node-type string but exposes a `function` field.

### B8. `codebase_rag/parsers/import_processor.py` — Kotlin import handler

Route `match language` to `_parse_kotlin_imports`. Walk each captured `import` node via `kotlin_utils.parse_import_header`. Three shapes to handle:
- plain `import a.b.C` → register `C → a.b.C`,
- aliased `import a.b.C as D` → register `D → a.b.C`,
- wildcard `import a.b.*` → register `*a.b → a.b`.

### B9. `codebase_rag/parsers/class_ingest/parent_extraction.py` — Kotlin supertypes

Add `extract_kotlin_supertypes` so `INHERITS` edges fire for `class Foo : Base()`, `object Singleton : Iface`, etc. Walk the `delegation_specifiers` (plural) wrapper, then each `delegation_specifier` child; supertypes appear as either a `constructor_invocation` (parent class call), a bare `user_type` (interface), or an `explicit_delegation` (`: Iface by impl`). Resolve each to a qualified name via `resolve_to_qn`.

## Verification

### Unit tests under `codebase_rag/tests/`

Phase A's `test_kotlin_smoke.py` covers the smallest case. Phase B adds 11 more, each writing a fixture into `temp_repo` and asserting via `get_node_names`/`get_relationships`:

| File | Phase B feature exercised |
|---|---|
| `test_kotlin_top_level_functions.py` | `package x.y; fun add(a: Int, b: Int): Int` attached to module; FQN includes parameter types |
| `test_kotlin_classes.py` | `class`, `open class`, `abstract class`, inheritance via `: Base()`, `INHERITS` edge, **method-inside-class extraction** (validates B1 hook) |
| `test_kotlin_data_sealed_value.py` | `data class User`, `sealed class Result`, `@JvmInline value class Email(val v: String)` modifiers extracted |
| `test_kotlin_companions_objects.py` | `class Foo { companion object { fun bar() {} } }`, `object Singleton { fun baz() {} }`, FQNs `Foo.Companion.bar`, `Singleton.baz`. Includes a regression test that asserts no `.Companion.Companion.` doubling. |
| `test_kotlin_extensions.py` | `fun String.shout(): String`, FQN includes receiver type |
| `test_kotlin_constructors.py` | primary + secondary constructors as separate function nodes |
| `test_kotlin_imports.py` | single, wildcard (`import kotlin.collections.*`), aliased (`import a.b.C as D`) |
| `test_kotlin_method_calls.py` | chained `a.b().c()`, infix `1 to 2`, scope functions `let/run/apply`; verifies the structured `call_query` doesn't emit spurious calls for property access |
| `test_kotlin_properties.py` | `val/var` with custom getters/setters become function nodes |
| `test_kotlin_overloading.py` | two `fn(x: Int)` and `fn(x: String)` disambiguated via parameter types in FQN |
| `test_kotlin_real_world.py` | small fixture corpus, count assertions, asserts no `ERROR` nodes via the warning-counter pattern from `test_java_real_world.py` |

### End-to-end on `ktorio/ktor-samples`

```bash
git clone --depth 1 https://github.com/ktorio/ktor-samples /tmp/ktor-samples
cgr index --repo-path /tmp/ktor-samples -o /tmp/ktor-index --split-index
```

Acceptance thresholds (validated via Cypher against the resulting graph):

- ≥1 `Module` per `.kt` file ingested (151 expected from the sample corpus).
- ≥10 `Class` nodes (Ktor samples are class-heavy).
- ≥50 `Function`/`Method` nodes combined.
- ≥20 `IMPORTS` edges (Ktor pulls heavily from `io.ktor.*`).
- ≥30 `CALLS` edges.
- 0 parse errors logged by the loader (use the `test_java_real_world.py` warning-counter assertion pattern).

Cypher confirmations:

```cypher
MATCH (m:Module) WHERE m.path ENDS WITH ".kt" RETURN count(m);
MATCH (c:Class)-[:DEFINED_IN]->(:Module {language: "kotlin"}) RETURN count(c);
MATCH (f:Function {name: "main"})-[:CALLS]->(g) RETURN g.qualified_name;
MATCH (c:Class)-[:CONTAINS]->(co:Class {name: "Companion"}) RETURN c.name, co.qualified_name;
```

Natural-language queries against the ingested graph:

```bash
cgr query "list all data classes in the kotlin project"
cgr query "what calls embeddedServer in this codebase"
cgr query "show all extension functions"
```

## Risks and mitigations

- **`mixin._ingest_class_methods` change**. Touching the generic class-ingest path is the riskiest edit. Mitigation: introduce the change as a non-breaking protocol method with a default that preserves today's behavior; verify by running the existing Java/Rust/Python class-ingest tests after the change.
- **Grammar parse errors on real-world Kotlin**. `tree-sitter-kotlin` has known weaknesses around exotic `when` exhaustiveness, complex string templates, and lambdas with trailing receivers. Treat `ERROR` nodes as warnings (Java pattern), don't fail tests on them.
- **Companion-member attribution**. `companion_object` is in both `SPEC_KOTLIN_CLASS_TYPES` (so it shows up as a Class node) and `FQN_KOTLIN_SCOPE_TYPES` (so its members get the right qualifier). The B1a filter ensures methods inside the companion are processed exactly once — when the companion itself is the class being walked, with `class_qn` already ending in `.Companion`. The handler trusts that and just appends `.method(params)`.
- **Extension-function attribution**. The default ingest path attaches free functions to the module. Folding the receiver into the function name inside `_kotlin_get_name` keeps changes local and produces `module.String.shout`; if a sharper attribution is needed (e.g., an `EXTENDS` edge), defer to a v2.
- **JS/TS call-target collision**. Kotlin's `call_expression` and `infix_expression` share node-type strings with JS — gate the Kotlin call-target branch on absence of the `function` and `name` fields so JS resolution still works.
- **Status flag**. Keep `LanguageStatus.DEV` until the 11 unit tests + e2e thresholds pass green on CI; promote to `FULL` only then.

## Critical files

Phase A:
- `pyproject.toml`
- `codebase_rag/constants.py`
- `codebase_rag/language_spec.py`
- `codebase_rag/parser_loader.py`
- `codebase_rag/tests/test_kotlin_smoke.py`

Phase B:
- `codebase_rag/parsers/handlers/protocol.py` (new method `find_class_body`)
- `codebase_rag/parsers/handlers/base.py` (default implementation)
- `codebase_rag/parsers/class_ingest/mixin.py` (body-lookup hook + `_is_direct_class_member` filter + Kotlin method-FQN branch)
- `codebase_rag/parsers/class_ingest/parent_extraction.py` (Kotlin `extract_kotlin_supertypes`)
- `codebase_rag/language_spec.py` (queries on `LANGUAGE_SPECS[KOTLIN]`, receiver folding in `_kotlin_get_name`, possibly extra modifier constants in `constants.py`)
- `codebase_rag/parsers/kotlin/__init__.py` (new)
- `codebase_rag/parsers/kotlin/utils.py` (new)
- `codebase_rag/parsers/handlers/kotlin.py` (new)
- `codebase_rag/parsers/handlers/registry.py`
- `codebase_rag/parsers/call_processor.py` (Kotlin call-target extraction in `_get_call_target_name`)
- `codebase_rag/parsers/import_processor.py` (Kotlin import handler)
- `codebase_rag/parsers/utils.py` (Kotlin name-extraction branch in `ingest_method`)
- `codebase_rag/tests/test_kotlin_*.py` (11 new files per the table above)
