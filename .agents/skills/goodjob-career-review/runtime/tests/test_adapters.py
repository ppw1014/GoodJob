from __future__ import annotations

import time

from goodjob.adapters import MAX_FACTS_PER_FILE, analyze_file


def _kinds(*, relative_path: str, text: str, artifact_kind: str, adapter_id: str) -> set[str]:
    return {
        fact.evidence_kind
        for fact in analyze_file(
            relative_path=relative_path,
            text=text,
            artifact_kind=artifact_kind,
            adapter_id=adapter_id,
            base_evidence_kind="implementation",
        ).facts
    }


def test_first_release_adapters_emit_structural_language_evidence() -> None:
    assert {
        "technology_usage",
        "symbol_definition",
        "capability_boundary",
        "routing",
        "entry_point",
    } <= _kinds(
        relative_path="service/main.py",
        text=(
            "import fastapi\n"
            "from jobs.worker import run\n"
            "app = fastapi.FastAPI()\n"
            "@app.get('/health')\n"
            "async def health():\n"
            "    return {}\n"
            "if __name__ == '__main__':\n"
            "    run()\n"
        ),
        artifact_kind="source",
        adapter_id="python",
    )

    assert {"technology_usage", "symbol_definition", "routing", "entry_point"} <= _kinds(
        relative_path="web/main.tsx",
        text=(
            "import { createRoot } from 'react-dom/client';\n"
            "export function App() { return <Route />; }\n"
            "createRoot(document.body).render(<App />);\n"
        ),
        artifact_kind="source",
        adapter_id="typescript",
    )
    assert {
        "technology_usage",
        "module_dependency",
        "symbol_definition",
        "capability_boundary",
        "entry_point",
        "test_definition",
    } <= _kinds(
        relative_path="src/main.rs",
        text=(
            "use tokio::runtime;\n"
            "mod api;\n"
            "async fn serve() {}\n"
            "fn main() {}\n"
            "#[cfg(test)] mod tests {}\n"
        ),
        artifact_kind="source",
        adapter_id="rust",
    )
    assert {"technology_usage", "symbol_definition", "routing", "entry_point"} <= _kinds(
        relative_path="lib/main.dart",
        text=(
            "import 'package:flutter/material.dart';\n"
            "class App extends StatelessWidget {}\n"
            "void main() { runApp(MaterialApp()); }\n"
        ),
        artifact_kind="source",
        adapter_id="dart",
    )
    assert {
        "schema_definition",
        "database_capability",
        "schema_relation",
        "schema_constraint",
    } <= _kinds(
        relative_path="migrations/001.sql",
        text=(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL "
            "REFERENCES users(id));\n"
            "CREATE UNIQUE INDEX orders_user_idx ON orders(user_id);\n"
        ),
        artifact_kind="source",
        adapter_id="sql",
    )


def test_cpp_adapter_emits_bounded_locatable_implementation_evidence() -> None:
    result = analyze_file(
        relative_path="src/main.cpp",
        text=(
            "#include <thread>\n"
            '#include "router.hpp"\n'
            "class Router { public: void serve(); };\n"
            "void Router::serve() { std::thread worker; }\n"
            "int main(int argc, char** argv) { return argc + (argv != nullptr); }\n"
        ),
        artifact_kind="source",
        adapter_id="cpp",
        base_evidence_kind="implementation",
    )

    kinds = {fact.evidence_kind for fact in result.facts}
    assert {"module_dependency", "technology_usage", "symbol_definition", "entry_point"} <= kinds
    assert "capability_boundary" in kinds
    assert not result.diagnostics
    assert all(
        "line" in dict(fact.locator_fields) or fact.evidence_kind == "implementation"
        for fact in result.facts
    )


def test_cpp_adapter_ignores_comments_strings_and_preprocessor_macros() -> None:
    result = analyze_file(
        relative_path="include/visible.hpp",
        text=(
            "// class Commented {};\n"
            'const char* text = "class StringFake {}; socket(";\n'
            "#define DECLARE_FAKE class MacroFake {}\n"
            "struct Visible { int value; };\n"
        ),
        artifact_kind="source",
        adapter_id="cpp",
        base_evidence_kind="implementation",
    )

    symbols = {
        dict(fact.locator_fields).get("symbol")
        for fact in result.facts
        if fact.evidence_kind == "symbol_definition"
    }
    assert "Visible" in symbols
    assert not {"Commented", "StringFake", "MacroFake"} & symbols
    assert "capability_boundary" not in {fact.evidence_kind for fact in result.facts}


def test_cpp_adapter_reports_parse_failure_and_fact_truncation() -> None:
    malformed = analyze_file(
        relative_path="broken.cc",
        text="int main() {\n",
        artifact_kind="source",
        adapter_id="cpp",
        base_evidence_kind="implementation",
    )
    assert malformed.facts == ()
    assert [diagnostic.kind for diagnostic in malformed.diagnostics] == ["source_parse_failed"]

    many_functions = analyze_file(
        relative_path="many.cpp",
        text="\n".join(f"void function_{index}() {{}}" for index in range(500)),
        artifact_kind="source",
        adapter_id="cpp",
        base_evidence_kind="implementation",
    )
    assert len(many_functions.facts) == MAX_FACTS_PER_FILE
    assert {diagnostic.kind for diagnostic in many_functions.diagnostics} == {"analysis_truncated"}


def test_cmake_manifest_uses_cpp_adapter_without_executing_cmake() -> None:
    result = analyze_file(
        relative_path="CMakeLists.txt",
        text="find_package(Threads)\nadd_executable(app main.cpp)\n",
        artifact_kind="manifest",
        adapter_id="cpp",
        base_evidence_kind="manifest",
    )

    assert not result.diagnostics
    assert {fact.evidence_kind for fact in result.facts} >= {
        "dependency_declaration",
        "entry_configuration",
        "module_boundary",
    }


def test_manifest_declaration_does_not_masquerade_as_actual_usage() -> None:
    kinds = _kinds(
        relative_path="package.json",
        text='{"dependencies":{"react":"1"},"scripts":{"build":"ignored command"}}',
        artifact_kind="manifest",
        adapter_id="typescript",
    )

    assert "dependency_declaration" in kinds
    assert "entry_configuration" in kinds
    assert "technology_usage" not in kinds


def test_adapter_output_is_bounded_even_for_many_symbols() -> None:
    result = analyze_file(
        relative_path="many.py",
        text="\n".join(f"def function_{index}(): pass" for index in range(500)),
        artifact_kind="source",
        adapter_id="python",
        base_evidence_kind="implementation",
    )

    assert len(result.facts) == MAX_FACTS_PER_FILE
    assert {diagnostic.kind for diagnostic in result.diagnostics} == {"analysis_truncated"}


def test_comment_text_does_not_create_language_or_schema_evidence() -> None:
    assert "technology_usage" not in _kinds(
        relative_path="app.ts",
        text="/* import hidden from 'secret-package'; */\nexport const visible = true;\n",
        artifact_kind="source",
        adapter_id="typescript",
    )
    assert "technology_usage" not in _kinds(
        relative_path="lib.rs",
        text=("fn borrow<'a>() {}\n/* use secret_crate::client; */\npub fn visible() {}\n"),
        artifact_kind="source",
        adapter_id="rust",
    )
    assert "schema_definition" not in _kinds(
        relative_path="query.sql",
        text="-- CREATE TABLE planned_only (id INTEGER);\nSELECT 1;\n",
        artifact_kind="source",
        adapter_id="sql",
    )


def test_supported_parse_failure_returns_a_diagnostic_without_implementation() -> None:
    result = analyze_file(
        relative_path="broken.py",
        text="def broken(:\n",
        artifact_kind="source",
        adapter_id="python",
        base_evidence_kind="implementation",
    )

    assert result.facts == ()
    assert {diagnostic.kind for diagnostic in result.diagnostics} == {"source_parse_failed"}


def test_typescript_import_analysis_is_linear_for_many_malformed_statements() -> None:
    started_at = time.monotonic()
    result = analyze_file(
        relative_path="adversarial.ts",
        text="import x\n" * 10_000,
        artifact_kind="source",
        adapter_id="typescript",
        base_evidence_kind="implementation",
    )

    assert time.monotonic() - started_at < 2.0
    assert len(result.facts) == 1

    started_at = time.monotonic()
    blank_result = analyze_file(
        relative_path="blank-lines.ts",
        text="\n" * 10_000 + "export const visible = true;\n",
        artifact_kind="source",
        adapter_id="typescript",
        base_evidence_kind="implementation",
    )

    assert time.monotonic() - started_at < 2.0
    assert "symbol_definition" in {fact.evidence_kind for fact in blank_result.facts}
