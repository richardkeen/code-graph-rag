from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from ...constants import SupportedLanguage
from .base import BaseLanguageHandler
from .cpp import CppHandler
from .frontend import CssHandler, HtmlHandler
from .java import JavaHandler
from .js_ts import JsTsHandler
from .kotlin import KotlinHandler
from .lua import LuaHandler
from .protocol import LanguageHandler
from .python import PythonHandler
from .rust import RustHandler

_HANDLERS: dict[SupportedLanguage, type[BaseLanguageHandler]] = {
    SupportedLanguage.PYTHON: PythonHandler,
    SupportedLanguage.JS: JsTsHandler,
    SupportedLanguage.TS: JsTsHandler,
    SupportedLanguage.CPP: CppHandler,
    SupportedLanguage.RUST: RustHandler,
    SupportedLanguage.JAVA: JavaHandler,
    SupportedLanguage.KOTLIN: KotlinHandler,
    SupportedLanguage.LUA: LuaHandler,
    SupportedLanguage.CSS: CssHandler,
    SupportedLanguage.HTML: HtmlHandler,
    SupportedLanguage.SCSS: CssHandler,
}

_DEFAULT_HANDLER = BaseLanguageHandler


@lru_cache(maxsize=16)
def get_handler(language: SupportedLanguage) -> LanguageHandler:
    handler_class = _HANDLERS.get(language, _DEFAULT_HANDLER)
    return handler_class()


def iter_handlers() -> Iterator[LanguageHandler]:
    """Yield each registered language handler exactly once.

    Used by post-pass orchestration that needs to fan out to every
    language's deferred resolution hooks (e.g. Kotlin's INHERITS /
    IMPLEMENTS resolver) without the orchestrator branching on the
    language token.
    """
    for language in _HANDLERS:
        yield get_handler(language)
