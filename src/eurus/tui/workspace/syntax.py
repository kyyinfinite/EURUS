from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from pygments.lexers import TextLexer, guess_lexer_for_filename
from pygments.util import ClassNotFound

MAX_LEXER_LINE = 2000
HighlightResult = Sequence[tuple[str, str]]

TOKEN_CLASSES: tuple[tuple[str, str], ...] = (
    ("token-comment", "class:tok-comment"),
    ("token-keyword", "class:tok-keyword"),
    ("token-string", "class:tok-string"),
    ("token-number", "class:tok-number"),
    ("token-name-function", "class:tok-name-function"),
    ("token-name-class", "class:tok-name-class"),
    ("token-operator", "class:tok-operator"),
    ("token-builtin", "class:tok-builtin"),
    ("token-decorator", "class:tok-decorator"),
    ("token-whitespace", "class:tok-whitespace"),
)

TOKEN_STYLE_KEYS = {
    "token-comment": "dim",
    "token-keyword": "copper",
    "token-string": "green",
    "token-number": "amber",
    "token-name-function": "steel",
    "token-name-class": "steel",
    "token-operator": "teal",
    "token-builtin": "teal",
    "token-decorator": "yellow",
    "token-whitespace": "text",
}

TOKEN_CLASS_BY_TYPE: dict[Any, str] = {
    None: "token-whitespace",
}


@lru_cache(maxsize=256)
def lexer_for(filename: str) -> Any:
    try:
        lexer = guess_lexer_for_filename(filename, "")
    except ClassNotFound:
        return None
    if isinstance(lexer, TextLexer):
        return None
    return lexer


def token_class(token_type: object) -> str:
    cached = TOKEN_CLASS_BY_TYPE.get(token_type)
    if cached is not None:
        return cached
    name = str(token_type)
    if "Comment" in name:
        resolved = "token-comment"
    elif "Keyword" in name:
        resolved = "token-keyword"
    elif "String" in name:
        resolved = "token-string"
    elif "Number" in name:
        resolved = "token-number"
    elif "Name.Function" in name or "Name.Function.Magic" in name:
        resolved = "token-name-function"
    elif "Name.Class" in name or "Name.Namespace" in name:
        resolved = "token-name-class"
    elif "Name.Builtin" in name:
        resolved = "token-builtin"
    elif "Name.Decorator" in name:
        resolved = "token-decorator"
    elif "Operator" in name:
        resolved = "token-operator"
    elif name.startswith("Token.Name"):
        resolved = "token-whitespace"
    else:
        resolved = "token-whitespace"
    TOKEN_CLASS_BY_TYPE[token_type] = resolved
    return resolved


def highlight_line(line: str, lexer: Any) -> list[tuple[str, str]]:
    if lexer is None or not line.strip():
        return [("class:tok-whitespace", line)]
    if len(line) > MAX_LEXER_LINE:
        return [("class:tok-whitespace", line)]
    try:
        tokens = list(lexer.get_tokens(line))
    except Exception:
        return [("class:tok-whitespace", line)]
    out: list[tuple[str, str]] = []
    for token_type, value in tokens:
        text = value.rstrip("\n")
        if not text:
            continue
        out.append((f"class:{token_class(token_type)}", text))
    if not out:
        out = [("class:tok-whitespace", line)]
    return out
