"""Bounded, non-executing source adapters for scan-time structural evidence."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FACTS_PER_FILE = 200
MAX_COLLECTED_FACTS_PER_FILE = MAX_FACTS_PER_FILE * 2
MAX_IDENTIFIER_LENGTH = 160
MAX_TYPESCRIPT_IMPORT_STATEMENT = 4096
ADAPTER_VERSIONS = {
    "cpp": "cpp-v1",
    "dart": "dart-v1",
    "generic": "generic-v1",
    "python": "python-v1",
    "rust": "rust-v1",
    "sql": "sql-v1",
    "typescript": "typescript-v1",
}


@dataclass(frozen=True)
class AnalysisFact:
    evidence_kind: str
    locator_fields: tuple[tuple[str, str | int], ...]
    summary: str

    def locator(self, relative_path: str) -> dict[str, str | int]:
        return {"relative_path": relative_path, **dict(self.locator_fields)}


@dataclass(frozen=True)
class AnalysisDiagnostic:
    kind: str
    message: str
    remediation: str


@dataclass(frozen=True)
class AnalysisResult:
    facts: tuple[AnalysisFact, ...]
    diagnostics: tuple[AnalysisDiagnostic, ...]


def _identifier(value: str) -> str | None:
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_IDENTIFIER_LENGTH:
        return None
    if any(ord(character) < 32 for character in normalized):
        return None
    return normalized


def _fact(
    evidence_kind: str,
    summary: str,
    *,
    line: int | None = None,
    **locator: str,
) -> AnalysisFact | None:
    fields: list[tuple[str, str | int]] = []
    for key, raw_value in sorted(locator.items()):
        value = _identifier(raw_value)
        if value is None:
            return None
        fields.append((key, value))
    if line is not None:
        fields.append(("line", line))
    return AnalysisFact(evidence_kind, tuple(fields), summary[:500])


def _deduplicate(facts: list[AnalysisFact]) -> tuple[tuple[AnalysisFact, ...], bool]:
    result: list[AnalysisFact] = []
    seen: set[tuple[str, tuple[tuple[str, str | int], ...]]] = set()
    for fact in facts:
        key = (fact.evidence_kind, fact.locator_fields)
        if key in seen:
            continue
        seen.add(key)
        result.append(fact)
        if len(result) == MAX_FACTS_PER_FILE:
            break
    unique_count = len({(fact.evidence_kind, fact.locator_fields) for fact in facts})
    return tuple(result), len(result) < unique_count


def _append(facts: list[AnalysisFact], fact: AnalysisFact | None) -> None:
    if fact is not None and len(facts) < MAX_COLLECTED_FACTS_PER_FILE:
        facts.append(fact)


def _line_starts(text: str) -> tuple[int, ...]:
    return (0, *(index + 1 for index, character in enumerate(text) if character == "\n"))


def _line_number(starts: tuple[int, ...], position: int) -> int:
    return bisect_right(starts, position)


def _python_facts(text: str) -> tuple[list[AnalysisFact], bool]:
    facts: list[AnalysisFact] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return facts, False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dependency = alias.name.split(".", 1)[0]
                _append(
                    facts,
                    _fact(
                        "technology_usage",
                        f"Imports Python dependency {dependency}.",
                        line=node.lineno,
                        dependency=dependency,
                    ),
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            dependency = node.module.split(".", 1)[0]
            _append(
                facts,
                _fact(
                    "module_dependency",
                    f"Imports from Python module {node.module}.",
                    line=node.lineno,
                    dependency=dependency,
                    module=node.module,
                ),
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbol_kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )
            _append(
                facts,
                _fact(
                    "symbol_definition",
                    f"Defines Python {symbol_kind} {node.name}.",
                    line=node.lineno,
                    symbol=node.name,
                    symbol_kind=symbol_kind,
                ),
            )
            if isinstance(node, ast.AsyncFunctionDef):
                _append(
                    facts,
                    _fact(
                        "capability_boundary",
                        f"Defines asynchronous Python boundary {node.name}.",
                        line=node.lineno,
                        symbol=node.name,
                        capability="async",
                    ),
                )
            for decorator in node.decorator_list:
                decorator_name = _python_decorator_name(decorator)
                if decorator_name and decorator_name.split(".")[-1] in {
                    "delete",
                    "get",
                    "patch",
                    "post",
                    "put",
                    "route",
                }:
                    _append(
                        facts,
                        _fact(
                            "routing",
                            f"Connects Python handler {node.name} through a route decorator.",
                            line=node.lineno,
                            symbol=node.name,
                            route_kind=decorator_name,
                        ),
                    )
        elif isinstance(node, ast.If) and _is_python_main_guard(node.test):
            _append(
                facts,
                _fact(
                    "entry_point",
                    "Defines a Python process entry point.",
                    line=node.lineno,
                    entry_kind="main_guard",
                ),
            )
    return facts, True


def _python_decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        return _python_decorator_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _python_decorator_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _is_python_main_guard(node: ast.expr) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
        return False
    left, right = node.left, node.comparators[0]
    return (
        isinstance(node.ops[0], ast.Eq)
        and isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    )


def _without_comments(text: str, *, line_marker: str, quote_characters: str = "'\"`") -> str:
    """Blank comments in one linear pass while preserving line and byte positions."""
    output = list(text)
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in quote_characters:
            quote = character
            index += 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            stop = len(text) if end < 0 else end + 2
            for position in range(index, stop):
                if output[position] not in {"\n", "\r"}:
                    output[position] = " "
            index = stop
            continue
        if text.startswith(line_marker, index):
            stop = text.find("\n", index + len(line_marker))
            if stop < 0:
                stop = len(text)
            for position in range(index, stop):
                output[position] = " "
            index = stop
            continue
        index += 1
    return "".join(output)


_TS_FROM_MODULE = re.compile(r"\bfrom\s*[\"']([^\"']+)")
_TS_SIDE_EFFECT_MODULE = re.compile(r"^[ \t]*import\s*[\"']([^\"']+)")
_TS_REQUIRE_MODULE = re.compile(r"\brequire\s*\(\s*[\"']([^\"']+)")
_TS_SYMBOL = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(class|function|interface|type|enum|const)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _typescript_imports(text: str) -> list[tuple[str, int]]:
    imports: list[tuple[str, int]] = []
    pending = ""
    pending_line = 0
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        for match in _TS_REQUIRE_MODULE.finditer(line):
            imports.append((match.group(1), line_number))
            if len(imports) == MAX_COLLECTED_FACTS_PER_FILE:
                return imports
        stripped = line.lstrip()
        if not pending and not stripped.startswith(("import ", "import\t", "export ")):
            continue
        if not pending:
            pending_line = line_number
        pending += line
        should_inspect = (
            "from" in line
            or "'" in line
            or '"' in line
            or ";" in line
            or len(pending) >= MAX_TYPESCRIPT_IMPORT_STATEMENT
        )
        if not should_inspect:
            continue
        module_match = _TS_SIDE_EFFECT_MODULE.search(pending) or _TS_FROM_MODULE.search(pending)
        if module_match is not None:
            imports.append((module_match.group(1), pending_line))
        if (
            module_match is not None
            or ";" in line
            or len(pending) >= MAX_TYPESCRIPT_IMPORT_STATEMENT
        ):
            pending = ""
            pending_line = 0
        if len(imports) == MAX_COLLECTED_FACTS_PER_FILE:
            return imports
    return imports


def _typescript_facts(text: str) -> list[AnalysisFact]:
    facts: list[AnalysisFact] = []
    text = _without_comments(text, line_marker="//")
    line_starts = _line_starts(text)
    for module, line_number in _typescript_imports(text):
        dependency = module if not module.startswith((".", "/")) else module.split("/", 2)[0]
        kind = "technology_usage" if not module.startswith((".", "/")) else "module_dependency"
        _append(
            facts,
            _fact(
                kind,
                f"Imports TypeScript module {module}.",
                line=line_number,
                dependency=dependency,
                module=module,
            ),
        )
    for match in _TS_SYMBOL.finditer(text):
        symbol_kind, symbol = match.groups()
        _append(
            facts,
            _fact(
                "symbol_definition",
                f"Defines TypeScript {symbol_kind} {symbol}.",
                line=_line_number(line_starts, match.start()),
                symbol=symbol,
                symbol_kind=symbol_kind,
            ),
        )
    for token, route_kind in (
        ("createRoot(", "react_root"),
        ("createBrowserRouter(", "router"),
        ("<Route", "route_component"),
        ("app.listen(", "server_listen"),
    ):
        position = text.find(token)
        if position >= 0:
            _append(
                facts,
                _fact(
                    "entry_point" if route_kind in {"react_root", "server_listen"} else "routing",
                    f"Connects a TypeScript {route_kind.replace('_', ' ')} boundary.",
                    line=_line_number(line_starts, position),
                    entry_kind=route_kind,
                ),
            )
    return facts


_CPP_INCLUDE = re.compile(
    r"^[ \t]*#[ \t]*include[ \t]*([<\"])([^>\"]+)[>\"]",
    re.MULTILINE,
)
_CPP_TYPE = re.compile(
    r"^[ \t]*(?:(?:template[ \t]*<[^{}\n]{0,200}>)[ \t]*)?"
    r"(?P<kind>class|struct|enum(?:[ \t]+class)?|union)[ \t]+"
    r"(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)
_CPP_FUNCTION = re.compile(
    r"^[ \t]*(?:(?:template[ \t]*<[^{}\n]{0,200}>)[ \t]*)?"
    r"(?:(?:[A-Za-z_~][\w:<>*& ,]*)[ \t]+)?"
    r"(?P<name>(?:(?:[A-Za-z_]\w*)::)*~?[A-Za-z_]\w*|operator[ \t]*[^\s(]+)"
    r"[ \t]*\([^;{}\n]{0,4096}\)[ \t]*"
    r"(?:const\b|constexpr\b|noexcept\b|override\b|final\b|&|&&|->[^{};]*)*"
    r"[ \t]*\{",
    re.MULTILINE,
)
_CPP_CONTROL_FUNCTION_NAMES = frozenset(
    {"catch", "for", "if", "switch", "while", "sizeof", "static_assert"}
)


def _mask_cpp_literals(text: str) -> str | None:
    """Blank C/C++ string and character literals while preserving line offsets."""
    output = list(text)
    index = 0
    while index < len(text):
        character = text[index]
        if character not in "'\"":
            index += 1
            continue
        quote = character
        output[index] = " "
        index += 1
        escaped = False
        while index < len(text):
            current = text[index]
            if current in "\n\r":
                if not escaped:
                    return None
                escaped = False
                index += 1
                continue
            if current != quote:
                output[index] = " "
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == quote:
                output[index] = " "
                index += 1
                break
            index += 1
        else:
            return None
    return "".join(output)


def _cpp_code_view(text: str) -> str | None:
    comment_free = _without_comments(text, line_marker="//", quote_characters="'\"")
    literal_free = _mask_cpp_literals(comment_free)
    if literal_free is None:
        return None
    output = list(literal_free)
    offset = 0
    continued_directive = False
    for line in literal_free.splitlines(keepends=True):
        is_directive = continued_directive or line.lstrip().startswith("#")
        if is_directive:
            for index in range(offset, offset + len(line)):
                if output[index] not in {"\n", "\r"}:
                    output[index] = " "
        physical_line = line.rstrip("\r\n").rstrip(" \t")
        continued_directive = is_directive and physical_line.endswith("\\")
        offset += len(line)
    return "".join(output)


def _without_cmake_comments(text: str) -> str:
    """Blank CMake line and bracket comments while preserving source positions."""
    output = list(text)
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        character = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            index += 1
            continue
        if character == '"':
            quoted = True
            index += 1
            continue
        if character != "#":
            index += 1
            continue
        bracket = re.match(r"#\[(=*)\[", text[index:])
        if bracket is not None:
            closing = "]" + bracket.group(1) + "]"
            end = text.find(closing, index + bracket.end())
            stop = len(text) if end < 0 else end + len(closing)
        else:
            end = text.find("\n", index + 1)
            stop = len(text) if end < 0 else end
        for position in range(index, stop):
            if output[position] not in {"\n", "\r"}:
                output[position] = " "
        index = stop
    return "".join(output)


def _cpp_delimiters_are_balanced(text: str) -> bool:
    pairs = {"}": "{", "]": "[", ")": "("}
    opening = set(pairs.values())
    stack: list[str] = []
    for character in text:
        if character in opening:
            stack.append(character)
        elif character in pairs and (not stack or stack.pop() != pairs[character]):
            return False
    return not stack


def _cpp_facts(text: str) -> tuple[list[AnalysisFact], bool]:
    facts: list[AnalysisFact] = []
    comment_free = _without_comments(text, line_marker="//", quote_characters="'\"")
    code_view = _cpp_code_view(text)
    if code_view is None or not _cpp_delimiters_are_balanced(code_view):
        return facts, False
    line_starts = _line_starts(code_view)

    for match in _CPP_INCLUDE.finditer(comment_free):
        delimiter, header = match.groups()
        dependency = header.split("/", 1)[0]
        _append(
            facts,
            _fact(
                "technology_usage" if delimiter == "<" else "module_dependency",
                f"Includes C/C++ header {header}.",
                line=_line_number(line_starts, match.start()),
                dependency=dependency,
                module=header,
            ),
        )
    for match in _CPP_TYPE.finditer(code_view):
        symbol_kind = "_".join(match.group("kind").split())
        symbol = match.group("name")
        _append(
            facts,
            _fact(
                "symbol_definition",
                f"Defines C/C++ {symbol_kind} {symbol}.",
                line=_line_number(line_starts, match.start()),
                symbol=symbol,
                symbol_kind=symbol_kind,
            ),
        )
    for match in _CPP_FUNCTION.finditer(code_view):
        symbol = match.group("name")
        simple_name = symbol.rsplit("::", 1)[-1]
        if simple_name in _CPP_CONTROL_FUNCTION_NAMES:
            continue
        _append(
            facts,
            _fact(
                "symbol_definition",
                f"Defines C/C++ function {symbol}.",
                line=_line_number(line_starts, match.start()),
                symbol=symbol,
                symbol_kind="function",
            ),
        )
        if simple_name == "main":
            _append(
                facts,
                _fact(
                    "entry_point",
                    "Defines a C/C++ process entry point.",
                    line=_line_number(line_starts, match.start()),
                    entry_kind="main",
                ),
            )
    for token, capability in (
        ("std::thread", "threading"),
        ("std::async", "async"),
        ("pthread_create", "threading"),
        ("socket(", "network"),
        ("connect(", "network"),
        ("accept(", "network"),
        ("fork(", "process"),
        ("dlopen(", "dynamic_loading"),
        ("sqlite3_open", "database"),
    ):
        position = code_view.find(token)
        if position >= 0:
            _append(
                facts,
                _fact(
                    "capability_boundary",
                    f"Uses a C/C++ {capability} capability.",
                    line=_line_number(line_starts, position),
                    capability=capability,
                ),
            )
    if re.search(r"^[ \t]*(?:TEST(?:_F|_P)?|TEST_CASE)[ \t]*\(", code_view, re.MULTILINE):
        _append(
            facts,
            _fact(
                "test_definition",
                "Defines C/C++ test cases.",
                test_kind="cpp",
            ),
        )
    return facts, True


_RUST_USE = re.compile(
    r"^[ \t]*(?:pub\s+)?(?:use|extern\s+crate)\s+([A-Za-z_][\w:]*)",
    re.MULTILINE,
)
_RUST_MOD = re.compile(
    r"^[ \t]*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_][\w]*)",
    re.MULTILINE,
)
_RUST_SYMBOL = re.compile(
    r"^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
    r"(fn|struct|enum|trait)\s+([A-Za-z_][\w]*)",
    re.MULTILINE,
)


def _rust_facts(text: str) -> list[AnalysisFact]:
    facts: list[AnalysisFact] = []
    text = _without_comments(text, line_marker="//", quote_characters='"')
    line_starts = _line_starts(text)
    for match in _RUST_USE.finditer(text):
        module = match.group(1)
        dependency = module.split("::", 1)[0]
        _append(
            facts,
            _fact(
                "technology_usage",
                f"Uses Rust crate or module {dependency}.",
                line=_line_number(line_starts, match.start()),
                dependency=dependency,
                module=module,
            ),
        )
    for match in _RUST_MOD.finditer(text):
        module = match.group(1)
        _append(
            facts,
            _fact(
                "module_dependency",
                f"Declares Rust module {module}.",
                line=_line_number(line_starts, match.start()),
                module=module,
            ),
        )
    for match in _RUST_SYMBOL.finditer(text):
        symbol_kind, symbol = match.groups()
        _append(
            facts,
            _fact(
                "symbol_definition",
                f"Defines Rust {symbol_kind} {symbol}.",
                line=_line_number(line_starts, match.start()),
                symbol=symbol,
                symbol_kind=symbol_kind,
            ),
        )
        if symbol == "main" and symbol_kind == "fn":
            _append(
                facts,
                _fact(
                    "entry_point",
                    "Defines a Rust binary entry point.",
                    line=_line_number(line_starts, match.start()),
                    entry_kind="main",
                ),
            )
    if "async fn" in text:
        _append(
            facts,
            _fact(
                "capability_boundary",
                "Contains asynchronous Rust boundaries.",
                capability="async",
            ),
        )
    if "#[cfg(test)]" in text or "#[test]" in text:
        _append(
            facts,
            _fact(
                "test_definition",
                "Defines Rust compile-time test coverage.",
                test_kind="rust",
            ),
        )
    return facts


_DART_IMPORT = re.compile(r"^[ \t]*import\s+[\"']([^\"']+)[\"']", re.MULTILINE)
_DART_SYMBOL = re.compile(
    r"^[ \t]*(?:abstract\s+)?(class|enum|mixin|extension)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _dart_facts(text: str) -> list[AnalysisFact]:
    facts: list[AnalysisFact] = []
    text = _without_comments(text, line_marker="//")
    line_starts = _line_starts(text)
    for match in _DART_IMPORT.finditer(text):
        module = match.group(1)
        dependency = module.removeprefix("package:").split("/", 1)[0]
        _append(
            facts,
            _fact(
                "technology_usage" if module.startswith("package:") else "module_dependency",
                f"Imports Dart library {module}.",
                line=_line_number(line_starts, match.start()),
                dependency=dependency,
                module=module,
            ),
        )
    for match in _DART_SYMBOL.finditer(text):
        symbol_kind, symbol = match.groups()
        _append(
            facts,
            _fact(
                "symbol_definition",
                f"Defines Dart {symbol_kind} {symbol}.",
                line=_line_number(line_starts, match.start()),
                symbol=symbol,
                symbol_kind=symbol_kind,
            ),
        )
    main_match = re.search(r"\b(?:Future<\s*void\s*>\s+|void\s+)?main\s*\(", text)
    if main_match:
        _append(
            facts,
            _fact(
                "entry_point",
                "Defines a Dart or Flutter application entry point.",
                line=_line_number(line_starts, main_match.start()),
                entry_kind="main",
            ),
        )
    for token, route_kind in (
        ("MaterialApp(", "material_app"),
        ("GoRoute(", "go_router"),
        ("Navigator.", "navigator"),
    ):
        position = text.find(token)
        if position >= 0:
            _append(
                facts,
                _fact(
                    "routing",
                    f"Connects Flutter navigation through {route_kind}.",
                    line=_line_number(line_starts, position),
                    route_kind=route_kind,
                ),
            )
    return facts


_SQL_OBJECT = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW|TRIGGER|(?:UNIQUE\s+)?INDEX)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`\[\]]+)",
    re.IGNORECASE,
)
_SQL_REFERENCE = re.compile(r"\bREFERENCES\s+([\w.\"`\[\]]+)", re.IGNORECASE)


def _sql_facts(text: str) -> list[AnalysisFact]:
    facts: list[AnalysisFact] = []
    text = _without_comments(text, line_marker="--")
    line_starts = _line_starts(text)
    for match in _SQL_OBJECT.finditer(text):
        object_kind = "_".join(match.group(1).lower().split())
        name = match.group(2).strip('"`[]')
        _append(
            facts,
            _fact(
                "schema_definition" if object_kind == "table" else "database_capability",
                f"Defines SQL {object_kind.replace('_', ' ')} {name}.",
                line=_line_number(line_starts, match.start()),
                object_kind=object_kind,
                symbol=name,
            ),
        )
    for match in _SQL_REFERENCE.finditer(text):
        target = match.group(1).strip('"`[]')
        _append(
            facts,
            _fact(
                "schema_relation",
                f"Declares a SQL relation to {target}.",
                line=_line_number(line_starts, match.start()),
                target=target,
            ),
        )
    for keyword in ("CHECK", "FOREIGN KEY", "NOT NULL", "UNIQUE"):
        constraint_match = re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE)
        if constraint_match:
            _append(
                facts,
                _fact(
                    "schema_constraint",
                    f"Uses SQL {keyword.lower()} constraints.",
                    line=_line_number(line_starts, constraint_match.start()),
                    constraint=keyword.lower().replace(" ", "_"),
                ),
            )
    return facts


def _manifest_facts(filename: str, text: str, adapter_id: str) -> tuple[list[AnalysisFact], bool]:
    facts: list[AnalysisFact] = []
    lower = filename.lower()
    if lower == "package.json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return facts, False
        if not isinstance(payload, dict):
            return facts, False
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            values = payload.get(section)
            if not isinstance(values, dict):
                continue
            for dependency in sorted(values):
                if not isinstance(dependency, str):
                    continue
                _append(
                    facts,
                    _fact(
                        "dependency_declaration",
                        f"Declares package dependency {dependency} in {section}.",
                        dependency=dependency,
                        dependency_scope=section,
                    ),
                )
        scripts = payload.get("scripts")
        if isinstance(scripts, dict):
            for script_name in sorted(scripts):
                if isinstance(script_name, str):
                    _append(
                        facts,
                        _fact(
                            "entry_configuration",
                            f"Declares package script {script_name}.",
                            entry_kind="package_script",
                            symbol=script_name,
                        ),
                    )
    elif lower in {"pyproject.toml", "cargo.toml"}:
        try:
            payload = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return facts, False
        if lower == "pyproject.toml":
            project = payload.get("project", {})
            if isinstance(project, dict):
                dependencies = project.get("dependencies", [])
                if isinstance(dependencies, list):
                    for raw_dependency in dependencies:
                        if isinstance(raw_dependency, str):
                            dependency = re.split(r"[<>=!~\s\[]", raw_dependency, maxsplit=1)[0]
                            _append(
                                facts,
                                _fact(
                                    "dependency_declaration",
                                    f"Declares Python dependency {dependency}.",
                                    dependency=dependency,
                                    dependency_scope="project",
                                ),
                            )
                scripts = project.get("scripts", {})
                if isinstance(scripts, dict):
                    for name in sorted(scripts):
                        if isinstance(name, str):
                            _append(
                                facts,
                                _fact(
                                    "entry_configuration",
                                    f"Declares Python command entry {name}.",
                                    entry_kind="project_script",
                                    symbol=name,
                                ),
                            )
        else:
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                values = payload.get(section, {})
                if isinstance(values, dict):
                    for dependency in sorted(values):
                        if isinstance(dependency, str):
                            _append(
                                facts,
                                _fact(
                                    "dependency_declaration",
                                    f"Declares Rust dependency {dependency} in {section}.",
                                    dependency=dependency,
                                    dependency_scope=section,
                                ),
                            )
    elif lower.startswith("requirements") and lower.endswith(".txt"):
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            dependency = re.split(r"[<>=!~\s\[]", line, maxsplit=1)[0]
            _append(
                facts,
                _fact(
                    "dependency_declaration",
                    f"Declares Python requirement {dependency}.",
                    dependency=dependency,
                    dependency_scope="requirements",
                ),
            )
    elif lower == "pubspec.yaml":
        pubspec_section: str | None = None
        for raw_line in text.splitlines():
            if raw_line and not raw_line.startswith((" ", "\t")):
                pubspec_section = raw_line.removesuffix(":") if raw_line.endswith(":") else None
                continue
            if pubspec_section not in {"dependencies", "dev_dependencies"}:
                continue
            pubspec_match = re.match(r"^\s{2,}([A-Za-z_][\w-]*):", raw_line)
            if pubspec_match:
                dependency = pubspec_match.group(1)
                _append(
                    facts,
                    _fact(
                        "dependency_declaration",
                        f"Declares Dart dependency {dependency} in {pubspec_section}.",
                        dependency=dependency,
                        dependency_scope=pubspec_section,
                    ),
                )
    elif lower == "cmakelists.txt":
        cmake_view = _without_cmake_comments(text)
        line_starts = _line_starts(cmake_view)
        for pattern, evidence_kind, summary in (
            (
                r"\bfind_package\s*\(",
                "dependency_declaration",
                "Declares a C/C++ package lookup in CMake.",
            ),
            (
                r"\badd_executable\s*\(",
                "entry_configuration",
                "Declares a C/C++ executable target in CMake.",
            ),
            (
                r"\badd_library\s*\(",
                "module_boundary",
                "Declares a C/C++ library target in CMake.",
            ),
            (
                r"\btarget_link_libraries\s*\(",
                "dependency_declaration",
                "Declares C/C++ target linkage in CMake.",
            ),
            (
                r"\badd_subdirectory\s*\(",
                "module_boundary",
                "Declares a C/C++ subdirectory boundary in CMake.",
            ),
        ):
            match = re.search(pattern, cmake_view)
            if match is not None:
                _append(
                    facts,
                    _fact(
                        evidence_kind,
                        summary,
                        line=_line_number(line_starts, match.start()),
                        build_system="cmake",
                    ),
                )
    if adapter_id != "generic":
        _append(
            facts,
            _fact(
                "module_boundary",
                f"Defines a {adapter_id} module manifest boundary.",
                module_kind=adapter_id,
            ),
        )
    return facts, True


def analyze_file(
    *,
    relative_path: str,
    text: str,
    artifact_kind: str,
    adapter_id: str,
    base_evidence_kind: str,
) -> AnalysisResult:
    """Return bounded structural facts without executing or retaining source text."""
    base_fact = AnalysisFact(
        base_evidence_kind,
        (),
        f"Indexes {artifact_kind} content with the {adapter_id} adapter.",
    )
    facts: list[AnalysisFact]
    parsed = True
    filename = PurePosixPath(relative_path).name
    if artifact_kind == "manifest":
        facts, parsed = _manifest_facts(filename, text, adapter_id)
    elif adapter_id == "python":
        facts, parsed = _python_facts(text)
    elif adapter_id == "typescript":
        facts = _typescript_facts(text)
    elif adapter_id == "cpp":
        facts, parsed = _cpp_facts(text)
    elif adapter_id == "rust":
        facts = _rust_facts(text)
    elif adapter_id == "dart":
        facts = _dart_facts(text)
    elif adapter_id == "sql":
        facts = _sql_facts(text)
    else:
        facts = []
    if not parsed:
        kind = "manifest_parse_failed" if artifact_kind == "manifest" else "source_parse_failed"
        return AnalysisResult(
            (),
            (
                AnalysisDiagnostic(
                    kind,
                    "A supported local adapter could not parse this file.",
                    "Fix the file syntax or exclude it, then run refresh.",
                ),
            ),
        )
    bounded_facts, truncated = _deduplicate(
        [
            base_fact,
            *facts,
        ]
    )
    diagnostics = (
        (
            AnalysisDiagnostic(
                "analysis_truncated",
                "A supported local adapter reached its per-file fact limit.",
                "Use later evidence-led reading for details omitted from the scan index.",
            ),
        )
        if truncated
        else ()
    )
    return AnalysisResult(bounded_facts, diagnostics)


def adapter_version(adapter_id: str) -> str:
    return ADAPTER_VERSIONS.get(adapter_id, ADAPTER_VERSIONS["generic"])
