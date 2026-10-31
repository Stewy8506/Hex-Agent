"""Active Graphify AST Indexer & Push-Based Blast-Radius Interceptor (PRD Sections 6.4, 11, FR-036, FR-036a, FR-038).

Builds a Python/repository AST symbol and call dependency graph, detects 1-hop and 2-hop downstream
callers, inheritors, and type dependents when an R2 tool modifies a file, injects an Architectural
Impact Warning, and dynamically appends impacted downstream test files to the task Verification Plan.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cow import IGNORE_DIRS

# Wire directly to vendor/graphify upstream engine if available
_VENDOR_GRAPHIFY = Path(__file__).resolve().parent.parent.parent / "vendor" / "graphify"
if _VENDOR_GRAPHIFY.is_dir() and str(_VENDOR_GRAPHIFY) not in sys.path:
    sys.path.insert(0, str(_VENDOR_GRAPHIFY))

try:
    import networkx as nx
    from graphify import affected as upstream_affected
    UPSTREAM_GRAPHIFY_AVAILABLE = True
except Exception:
    UPSTREAM_GRAPHIFY_AVAILABLE = False
    nx = None
    upstream_affected = None


@dataclass
class SymbolDef:
    name: str
    qualified_name: str
    kind: str  # 'function' | 'class' | 'method'
    rel_path: str
    lineno: int
    end_lineno: int
    calls: set[str] = field(default_factory=set)
    bases: set[str] = field(default_factory=set)


@dataclass
class BlastRadiusReport:
    modified_files: list[str]
    modified_symbols: list[str]
    hop1_dependents: list[dict[str, Any]]
    hop2_dependents: list[dict[str, Any]]
    impacted_tests: list[str]
    warning_message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "modified_files": self.modified_files,
            "modified_symbols": self.modified_symbols,
            "hop1_dependents": self.hop1_dependents,
            "hop2_dependents": self.hop2_dependents,
            "impacted_tests": self.impacted_tests,
            "warning_message": self.warning_message,
        }


class ActiveGraphifyAdapter:
    """Repository AST/call-graph index and Active Blast-Radius Hook."""

    def __init__(self, workspace_root: Path | str):
        self.workspace_root = Path(workspace_root).resolve()
        self.symbols_by_file: dict[str, list[SymbolDef]] = {}
        self.symbols_by_name: dict[str, list[SymbolDef]] = {}
        self.callers_of: dict[str, set[str]] = {}  # symbol_name -> set of caller qualified_names
        self.file_of_qualified: dict[str, str] = {}
        self._file_hashes: dict[str, int] = {}
        self.upstream_available = UPSTREAM_GRAPHIFY_AVAILABLE
        self.graph: Any = None

    def build_or_refresh(self, workspace_dir: Path | str | None = None) -> dict[str, Any]:
        root = Path(workspace_dir or self.workspace_root).resolve()
        self.workspace_root = root
        self.symbols_by_file.clear()
        self.symbols_by_name.clear()
        self.callers_of.clear()
        self.file_of_qualified.clear()

        py_files: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.startswith("."))
            for fname in sorted(filenames):
                if fname.endswith(".py"):
                    fpath = Path(dirpath) / fname
                    rel = str(fpath.relative_to(root)).replace("\\", "/")
                    py_files.append((rel, fpath))

        for rel, fpath in py_files:
            self._index_file(rel, fpath)

        # Build reverse call/inheritance edge table
        for rel, sym_list in self.symbols_by_file.items():
            for sym in sym_list:
                self.file_of_qualified[sym.qualified_name] = rel
                for called in sym.calls | sym.bases:
                    self.callers_of.setdefault(called, set()).add(sym.qualified_name)

        # Build upstream Graphify NetworkX directed graph representation
        if self.upstream_available and nx is not None:
            self.graph = nx.DiGraph()
            for rel, sym_list in self.symbols_by_file.items():
                self.graph.add_node(rel, kind="file", label=rel)
                for sym in sym_list:
                    self.graph.add_node(sym.qualified_name, kind=sym.kind, file=rel, label=sym.name)
                    self.graph.add_edge(rel, sym.qualified_name, relation="defines")
                    if sym.name != sym.qualified_name:
                        self.graph.add_node(sym.name, kind=sym.kind, file=rel, label=sym.name)
                        self.graph.add_edge(rel, sym.name, relation="defines")
                        self.graph.add_edge(sym.qualified_name, sym.name, relation="defines")
                    for called in sym.calls:
                        self.graph.add_node(called, kind="external", label=called)
                        self.graph.add_edge(sym.qualified_name, called, relation="calls")
                        if sym.name != sym.qualified_name:
                            self.graph.add_edge(sym.name, called, relation="calls")
                    for base in sym.bases:
                        self.graph.add_node(base, kind="class", label=base)
                        self.graph.add_edge(sym.qualified_name, base, relation="inherits")

        return {
            "indexed_files": len(py_files),
            "indexed_symbols": sum(len(v) for v in self.symbols_by_file.values()),
            "workspace_root": str(root),
            "upstream_graphify_active": bool(self.upstream_available and self.graph is not None),
        }

    def _index_file(self, rel_path: str, fpath: Path) -> None:
        try:
            src = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=rel_path)
        except SyntaxError:
            return

        mod_prefix = rel_path[:-3].replace("/", ".")
        defs: list[SymbolDef] = []

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym = self._extract_func(node, mod_prefix, rel_path, class_name=None)
                defs.append(sym)
            elif isinstance(node, ast.ClassDef):
                qname = f"{mod_prefix}.{node.name}"
                bases = {self._expr_name(b) for b in node.bases if self._expr_name(b)}
                calls = self._collect_calls(node)
                cls_sym = SymbolDef(
                    name=node.name,
                    qualified_name=qname,
                    kind="class",
                    rel_path=rel_path,
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno),
                    calls=calls,
                    bases=bases,
                )
                defs.append(cls_sym)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        msym = self._extract_func(item, mod_prefix, rel_path, class_name=node.name)
                        defs.append(msym)

        # Also capture top-level module calls and imports (e.g., in scripts or test modules)
        top_calls: set[str] = set()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                top_calls |= self._collect_calls(node)
        for child in ast.walk(tree):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                top_calls |= self._collect_imports(child)
        if top_calls:
            mod_sym = SymbolDef(
                name=Path(rel_path).stem,
                qualified_name=mod_prefix,
                kind="module",
                rel_path=rel_path,
                lineno=1,
                end_lineno=len(src.splitlines()) or 1,
                calls=top_calls,
            )
            defs.append(mod_sym)

        self.symbols_by_file[rel_path] = defs
        for d in defs:
            self.symbols_by_name.setdefault(d.name, []).append(d)

    def _collect_imports(self, node: ast.Import | ast.ImportFrom) -> set[str]:
        imported: set[str] = set()
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            for alias in node.names:
                imported.add(alias.name)
        return imported

    def _extract_func(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        mod_prefix: str,
        rel_path: str,
        class_name: str | None,
    ) -> SymbolDef:
        qname = f"{mod_prefix}.{class_name}.{node.name}" if class_name else f"{mod_prefix}.{node.name}"
        return SymbolDef(
            name=node.name,
            qualified_name=qname,
            kind="method" if class_name else "function",
            rel_path=rel_path,
            lineno=node.lineno,
            end_lineno=getattr(node, "end_lineno", node.lineno),
            calls=self._collect_calls(node),
        )

    def _collect_calls(self, root_node: ast.AST) -> set[str]:
        called: set[str] = set()
        for child in ast.walk(root_node):
            if isinstance(child, ast.Call):
                name = self._expr_name(child.func)
                if name:
                    called.add(name.split(".")[-1])
        return called

    def _expr_name(self, expr: ast.AST) -> str:
        if isinstance(expr, ast.Name):
            return expr.id
        if isinstance(expr, ast.Attribute):
            prefix = self._expr_name(expr.value)
            return f"{prefix}.{expr.attr}" if prefix else expr.attr
        return ""

    def query_symbol(self, symbol_name: str) -> dict[str, Any]:
        defs = self.symbols_by_name.get(symbol_name, [])
        hop1_qnames = sorted(self.callers_of.get(symbol_name, set()))
        hop2_qnames: set[str] = set()
        for qn in hop1_qnames:
            short = qn.split(".")[-1]
            hop2_qnames |= self.callers_of.get(short, set())
        hop2_qnames -= set(hop1_qnames)

        return {
            "symbol": symbol_name,
            "definitions": [
                {
                    "qualified_name": d.qualified_name,
                    "kind": d.kind,
                    "file": d.rel_path,
                    "lineno": d.lineno,
                }
                for d in defs
            ],
            "hop1_callers": [
                {"qualified_name": qn, "file": self.file_of_qualified.get(qn, "")}
                for qn in hop1_qnames
            ],
            "hop2_callers": [
                {"qualified_name": qn, "file": self.file_of_qualified.get(qn, "")}
                for qn in sorted(hop2_qnames)
            ],
        }

    def compute_blast_radius(
        self,
        workspace_dir: Path | str,
        modified_rel_paths: list[str],
        before_sources: dict[str, str] | None = None,
    ) -> BlastRadiusReport:
        """FR-036a: Computes 1-hop and 2-hop downstream call & test impact after R2 file modification."""
        self.build_or_refresh(workspace_dir)
        norm_paths = [p.replace("\\", "/").lstrip("./") for p in modified_rel_paths]

        modified_symbols: list[str] = []
        for rel in norm_paths:
            syms = self.symbols_by_file.get(rel, [])
            prev_src = (before_sources or {}).get(rel)
            if prev_src is not None:
                # Identify specifically mutated symbol bodies if previous source was provided
                changed = self._diff_modified_symbols(prev_src, (Path(workspace_dir) / rel).read_text(encoding="utf-8", errors="replace"))
                if changed:
                    modified_symbols.extend(changed)
                    continue
            for s in syms:
                if s.kind != "module" and s.name not in modified_symbols:
                    modified_symbols.append(s.name)

        hop1_map: dict[str, dict[str, Any]] = {}
        hop2_map: dict[str, dict[str, Any]] = {}
        impacted_tests: set[str] = set()

        # Interrogate upstream Graphify affected engine if active
        if self.upstream_available and self.graph is not None and upstream_affected is not None:
            for sym_name in modified_symbols:
                try:
                    hits = upstream_affected.affected_nodes(self.graph, seed=sym_name, depth=2)
                    for hit in hits:
                        if hit.via_relation == "defines":
                            continue
                        node_id = hit.node_id
                        node_data = self.graph.nodes.get(node_id, {})
                        node_file = node_data.get("file") or (node_id if node_id.endswith(".py") else self.file_of_qualified.get(node_id, ""))
                        if hit.depth == 1:
                            if node_id not in hop1_map:
                                hop1_map[node_id] = {
                                    "qualified_name": node_id,
                                    "file": node_file,
                                    "via_symbol": sym_name,
                                }
                        elif hit.depth == 2:
                            if node_id not in hop1_map and node_id not in hop2_map:
                                hop2_map[node_id] = {
                                    "qualified_name": node_id,
                                    "file": node_file,
                                    "via_hop1": sym_name,
                                }
                        if node_file and self._is_test_file(node_file):
                            impacted_tests.add(node_file)
                        elif self._is_test_file(node_id):
                            impacted_tests.add(node_id)
                except Exception:
                    pass

        for sym_name in modified_symbols:
            for caller_qn in sorted(self.callers_of.get(sym_name, set())):
                caller_file = self.file_of_qualified.get(caller_qn, "")
                hop1_map[caller_qn] = {
                    "qualified_name": caller_qn,
                    "file": caller_file,
                    "via_symbol": sym_name,
                }
                if self._is_test_file(caller_file):
                    impacted_tests.add(caller_file)

                caller_short = caller_qn.split(".")[-1]
                for h2_qn in sorted(self.callers_of.get(caller_short, set())):
                    if h2_qn not in hop1_map:
                        h2_file = self.file_of_qualified.get(h2_qn, "")
                        hop2_map[h2_qn] = {
                            "qualified_name": h2_qn,
                            "file": h2_file,
                            "via_hop1": caller_qn,
                        }
                        if self._is_test_file(h2_file):
                            impacted_tests.add(h2_file)

        hop1_list = list(hop1_map.values())
        hop2_list = list(hop2_map.values())

        # Expand 1 more hop specifically for test methods/classes that call 2-hop dependents
        for h2_item in hop2_list:
            h2_short = h2_item["qualified_name"].split(".")[-1]
            for h3_qn in sorted(self.callers_of.get(h2_short, set())):
                h3_file = self.file_of_qualified.get(h3_qn, "")
                if self._is_test_file(h3_file):
                    impacted_tests.add(h3_file)

        # Also check if any test file imports/mentions the modified module stem or hop1/hop2 symbols
        hop_symbols = set(modified_symbols) | {x["qualified_name"].split(".")[-1] for x in hop1_list + hop2_list}
        for rel in norm_paths:
            stem = Path(rel).stem
            for f_rel, syms in self.symbols_by_file.items():
                if self._is_test_file(f_rel):
                    for s in syms:
                        if stem in s.calls or any(ms in s.calls for ms in hop_symbols):
                            impacted_tests.add(f_rel)

        tests_list = sorted(impacted_tests)

        if modified_symbols:
            warning = (
                f"[AST Blast-Radius]: Modified symbol(s) {', '.join(modified_symbols)} in "
                f"{', '.join(norm_paths)}; impacts {len(hop1_list)} 1-hop caller(s), "
                f"{len(hop2_list)} 2-hop dependent(s), and {len(tests_list)} test file(s)"
                + (f" ({', '.join(tests_list)})" if tests_list else "")
                + "."
            )
        else:
            warning = f"[AST Blast-Radius]: Modified {', '.join(norm_paths)} (no symbol delta detected)."

        return BlastRadiusReport(
            modified_files=norm_paths,
            modified_symbols=modified_symbols,
            hop1_dependents=hop1_list,
            hop2_dependents=hop2_list,
            impacted_tests=tests_list,
            warning_message=warning,
        )

    def _diff_modified_symbols(self, before_src: str, after_src: str) -> list[str]:
        try:
            t1 = ast.parse(before_src)
            t2 = ast.parse(after_src)
        except SyntaxError:
            return []

        def _extract_defs_text(src: str, tree: ast.Module) -> dict[str, str]:
            out: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    seg = ast.get_source_segment(src, node)
                    out[node.name] = seg if seg is not None else ast.dump(node, include_attributes=False)
            return out

        d1 = _extract_defs_text(before_src, t1)
        d2 = _extract_defs_text(after_src, t2)
        changed = [name for name, seg in d2.items() if d1.get(name) != seg]
        removed = [name for name in d1 if name not in d2]
        return sorted(set(changed + removed))

    def _is_test_file(self, rel_path: str) -> bool:
        p = Path(rel_path)
        return p.name.startswith("test_") or p.name.endswith("_test.py") or "tests/" in rel_path
