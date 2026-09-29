"""A small, error-tolerant JavaScript tokenizer for ServiceNow scripts.

ServiceNow scripts mix ES5 and newer syntax, embedded Jelly/AngularJS fragments and
occasionally incomplete code. Rules therefore work on tokens, with comments and string
contents separated from code, rather than on a strict AST: every script can be analysed
and text inside comments or strings never matches as code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KEYWORDS_BEFORE_EXPRESSION = frozenset({
    "return", "typeof", "case", "do", "else", "in", "instanceof", "new", "delete", "void",
    "throw", "yield", "await", "of",
})
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
NUMBER = re.compile(r"(?:0[xX][0-9a-fA-F]+|\d[\d_]*(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)n?")
PUNCT = re.compile(
    r">>>=|\.\.\.|===|!==|\*\*=|<<=|>>=|>>>|&&=|\|\|=|\?\?=|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\."
    r"|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<|>>|\*\*|[{}()\[\];,<>+\-*/%&|^!~?:=.@#]"
)


@dataclass(frozen=True)
class Token:
    kind: str  # ident | string | template | number | punct | regex
    value: str
    line: int


@dataclass(frozen=True)
class Comment:
    text: str
    line: int


@dataclass
class Script:
    tokens: list[Token]
    comments: list[Comment]
    pairs: dict[int, int] = field(default_factory=dict)

    def match(self, index: int) -> int | None:
        return self.pairs.get(index)

    def is_call(self, index: int, name: str) -> bool:
        """``name(`` at ``index``, not preceded by ``.`` (a free function call)."""

        tokens = self.tokens
        return (
            tokens[index].kind == "ident" and tokens[index].value == name
            and index + 1 < len(tokens) and tokens[index + 1].value == "("
            and not (index > 0 and tokens[index - 1].value in {".", "?."})
        )

    def is_member_call(self, index: int, method: str) -> bool:
        """``.method(`` with the method identifier at ``index``."""

        tokens = self.tokens
        return (
            tokens[index].kind == "ident" and tokens[index].value == method
            and index > 0 and tokens[index - 1].value in {".", "?."}
            and index + 1 < len(tokens) and tokens[index + 1].value == "("
        )

    def receiver(self, index: int) -> str | None:
        """Identifier immediately before ``.`` for a member at ``index``."""

        if index >= 2 and self.tokens[index - 2].kind == "ident":
            return self.tokens[index - 2].value
        return None

    def arguments(self, open_index: int) -> list[list[Token]]:
        """Top-level comma-separated arguments of the call whose ``(`` is at ``open_index``."""

        close = self.match(open_index)
        if close is None:
            return []
        args: list[list[Token]] = [[]]
        index = open_index + 1
        while index < close:
            token = self.tokens[index]
            if token.value in {"(", "[", "{"} and token.kind == "punct":
                end = self.match(index) or index
                args[-1].extend(self.tokens[index:end + 1])
                index = end + 1
                continue
            if token.kind == "punct" and token.value == ",":
                args.append([])
            else:
                args[-1].append(token)
            index += 1
        return [arg for arg in args if arg]


def _read_quoted(source: str, start: int, quote: str) -> tuple[str, int]:
    index = start + 1
    out: list[str] = []
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\" and index + 1 < length:
            out.append(source[index + 1])
            index += 2
            continue
        if char == quote or (char == "\n" and quote != "`"):
            return "".join(out), index + 1
        if quote == "`" and source.startswith("${", index):
            depth = 0
            while index < length:
                if source[index] == "{":
                    depth += 1
                elif source[index] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                out.append(source[index])
                index += 1
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out), index


def _read_regex(source: str, start: int) -> int:
    index = start + 1
    in_class = False
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            return index
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < length and (source[index].isalnum() or source[index] == "_"):
                index += 1
            return index
        index += 1
    return index


def _regex_allowed(previous: Token | None) -> bool:
    if previous is None:
        return True
    if previous.kind == "punct":
        return previous.value not in {")", "]", "}"}
    if previous.kind == "ident":
        return previous.value in KEYWORDS_BEFORE_EXPRESSION
    return False


def tokenize(source: str) -> Script:
    tokens: list[Token] = []
    comments: list[Comment] = []
    index = 0
    line = 1
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end < 0 else end
            comments.append(Comment(source[index + 2:end].strip(), line))
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end < 0 else end + 2
            text = source[index:end]
            comments.append(Comment(text[2:-2].strip(), line))
            line += text.count("\n")
            index = end
            continue
        if char in "'\"`":
            value, end = _read_quoted(source, index, char)
            tokens.append(Token("template" if char == "`" else "string", value, line))
            line += source.count("\n", index, end)
            index = end
            continue
        if char == "/" and _regex_allowed(tokens[-1] if tokens else None):
            end = _read_regex(source, index)
            tokens.append(Token("regex", source[index:end], line))
            index = end
            continue
        match = IDENT.match(source, index)
        if match:
            tokens.append(Token("ident", match.group(), line))
            index = match.end()
            continue
        if char.isdigit() or (char == "." and index + 1 < length and source[index + 1].isdigit()):
            match = NUMBER.match(source, index)
            if match:
                tokens.append(Token("number", match.group(), line))
                index = match.end()
                continue
        match = PUNCT.match(source, index)
        if match:
            tokens.append(Token("punct", match.group(), line))
            index = match.end()
            continue
        index += 1  # unknown character (for example Jelly markup): skip it
    return Script(tokens, comments, _pair_brackets(tokens))


def _pair_brackets(tokens: list[Token]) -> dict[int, int]:
    pairs: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for index, token in enumerate(tokens):
        if token.kind != "punct":
            continue
        if token.value in {"(", "[", "{"}:
            stack.append((token.value, index))
        elif token.value in closing:
            want = closing[token.value]
            if not any(opened == want for opened, _ in stack):
                continue
            while stack[-1][0] != want:
                stack.pop()
            _, opened_at = stack.pop()
            pairs[opened_at] = index
            pairs[index] = opened_at
    return pairs
