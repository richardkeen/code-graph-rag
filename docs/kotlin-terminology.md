# Kotlin terminology mapping

This document explains how Kotlin language constructs and Android/Gradle build concepts map onto the graph's language-agnostic schema.

## Why this matters

The graph uses node labels that don't always match Kotlin or Gradle vernacular. Most notably:

- **`Module`** in this graph is a single Kotlin source file (the AST root), but **"module"** in Gradle/Kotlin parlance is a build unit (a directory with `build.gradle.kts`).
- **`Package`** in this graph is a build/dependency boundary, but **"package"** in Kotlin is the namespace declared at the top of a file (`package net.foo.bar`). The latter is recorded inside `qualified_name` strings, not as its own node type.

The tables below disambiguate.

## Node types

| Concept | Graph term | Kotlin / Gradle term |
|---|---|---|
| Repository or top-level project | `Project` | Gradle root project |
| Build/dependency boundary | `Package` | Gradle module — directory containing `build.gradle.kts` |
| Source directory (no build file) | `Folder` | source-set directory under `src/main/kotlin/...`, or any subdirectory |
| Any non-source file on disk | `File` | file on disk (`build.gradle.kts`, `README.md`, resources, …) |
| Source file / compilation unit | `Module` | Kotlin file (`.kt`, `.kts`) — the tree-sitter `source_file` AST root |
| Class-like declaration | `Class` | `class`, `object`, `companion object`, `interface`, `data class`, `sealed class`, `value class`, `typealias` |
| Enum class | `Enum` | `enum class` (individual `enum_entry` constants are not yet represented as graph nodes) |
| Top-level function | `Function` | top-level function or extension function |
| Member function | `Method` | member function inside a class/object/companion; constructors; property getters and setters |
| Lambda / anonymous function | `AnonymousFunction` | lambda expression or `anonymous_function` |
| External dependency | `ExternalPackage` | (not currently populated for Kotlin — `pyproject.toml` is the only manifest the dependency parser handles today) |

### Notes on overloaded mappings

- Kotlin `interface` declarations almost always map to **`Class`** (because tree-sitter-kotlin uses `class_declaration` for them). A small minority appear as `Interface` nodes when they are named in a context that triggers the interface-specific resolver (e.g. when another class explicitly `:` implements them and the parent-extraction pass detects an interface-like shape). Treat `Interface` as a rare, opportunistic refinement — the canonical Kotlin interface representation in this graph is `Class`.
- Kotlin `enum class` declarations map to **`Enum`** — the classifier inspects the `enum` modifier on `class_declaration` to make the distinction. Individual enum constants (`enum_entry` nodes — e.g. `RED`, `GREEN`, `BLUE`) are not yet represented as graph nodes; only the enum class itself is.
- `typealias` is represented as `Class` in this graph.

## Node types not used by Kotlin

| Graph term | Why |
|---|---|
| `Union` | Concept not in Kotlin |
| `Type` | Kotlin typealiases map to `Class`, not `Type` |
| `ModuleInterface` / `ModuleImplementation` | TypeScript / C++20 module-system concepts |
| `ExternalPackage` | Kotlin dependencies (Gradle declarations) are not yet ingested |

## Relationships

| Concept | Graph term | Notes |
|---|---|---|
| Project contains a Gradle module | `(Project)-[:CONTAINS_PACKAGE]->(Package)` | Triggered by presence of `build.gradle.kts` in a directory |
| Gradle module contains a sub-Gradle module | `(Package)-[:CONTAINS_PACKAGE]->(Package)` | Nested Gradle modules |
| Gradle module contains a sub-directory | `(Package)-[:CONTAINS_FOLDER]->(Folder)` | e.g. `src/main/kotlin` |
| Gradle module contains its own build file | `(Package)-[:CONTAINS_FILE]->(File)` | `build.gradle.kts` itself, plus other root-level files |
| Folder contains a Kotlin file | `(Folder)-[:CONTAINS_MODULE]->(Module)` | One `Module` per `.kt` file |
| File top-level definitions | `(Module)-[:DEFINES]->(Class\|Function\|AnonymousFunction)` | Top-level functions, classes, objects, lambdas |
| Class methods | `(Class)-[:DEFINES_METHOD]->(Method)` | Includes companion-object methods and property accessors |
| Inheritance | `(Class)-[:INHERITS]->(Class)` | Kotlin `: ParentClass(...)` and `: SomeInterface` both produce `INHERITS` edges in the common case |
| Interface implementation | `(Class)-[:IMPLEMENTS]->(Interface)` | Rare — only used when the parent has been refined to an `Interface` node |
| Method override | `(Method)-[:OVERRIDES]->(Method)` | Kotlin `override fun ...` |
| Import | `(Module)-[:IMPORTS]->(Module)` | Kotlin `import …` — resolved when the target module exists in the graph |
| Function or method call | `(Function\|Method\|Module)-[:CALLS]->(Function\|Method)` | Module-level callers exist for top-level call sites; lambda call sites are attributed to their host function |

## Notable simplifications

- **Companion objects** are represented as a `Class` named `Companion`, attached to the enclosing class via `DEFINES_METHOD` for its members.
- **Extension functions** are top-level `Function` nodes; the receiver type is encoded inside `qualified_name` rather than as a structural relationship.
- **Sealed-class hierarchies** use `INHERITS` edges from each variant `Class` back to the sealed parent.
- **Data, sealed, value, and inline classes** receive no special treatment — they are `Class` nodes like any other, distinguishable only by inspecting their `decorators` property.
- **Property getters/setters** are stored as `Method` nodes.

## See also

- [Kotlin support plan](kotlin-support-plan.md) — design and implementation plan for Kotlin support
- The Graph Schema section of the [README](../README.md#-graph-schema) — language-agnostic node and relationship definitions
