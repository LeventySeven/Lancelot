"""M6 T3.2 — MCP server exposing Lancelot's docs as a queryable resource.

Once registered in an MCP client (Claude Code, Cursor with MCP, etc.), the
client can answer "how do I do X in Lancelot?" from the actual source of
truth — not from the model's training-data snapshot. Closes the
LLM-recommendation-flywheel loop described in the YC Root Access
transcripts (Trigger.dev "two users now: human + LLM"; MCP-tool
minimal-return discipline per Dex Horthy / HumanLayer).

Soft dep: requires `pip install 'mcp[cli]'` — not in pyproject's runtime
deps (cardinal rule: pydantic-only). Lancelot itself doesn't depend on
this; the docs server is a separate utility.

Tools exposed:
  - lancelot_docs_list()  → returns the list of doc files (paths + titles)
  - lancelot_docs_get(path) → returns the content of one doc file
  - lancelot_docs_search(query, limit=5) → grep-style search across docs
                                            returns minimal snippets only
                                            (per Dex Horthy: MCP tools that
                                            return big JSON blobs flood the
                                            agent's context window — keep
                                            returns small + precise)

Run (stdio transport, the default for local agent clients):
  python tools/mcp_docs_server.py

Register in Claude Code (~/.config/claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "lancelot-docs": {
        "command": "/path/to/.venv/bin/python",
        "args": ["/path/to/lancelot/tools/mcp_docs_server.py"]
      }
    }
  }
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp[cli] not installed. Run: pip install 'mcp[cli]'",
          file=sys.stderr)
    sys.exit(2)


# Repo root = parent of tools/ dir.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Curated allow-list of doc paths exposed via MCP. We don't expose
# arbitrary repo files — only the docs we want agents to consume.
# Per-file size cap below caps any single read at 32 KB (Dex Horthy:
# MCP tools should return small precise snippets, not big JSON blobs).
DOC_PATHS: list[tuple[str, str]] = [
    # (relative_path, human_title)
    ("CLAUDE.md", "Project engineering notes — positioning, cardinal rules, current state"),
    ("docs/agents.md", "Lancelot conventions for code agents (read this first)"),
    ("docs/deploy.md", "Production deploy recipe (multiprocess SO_REUSEPORT)"),
    ("docs/migration/from-fastapi.md", "FastAPI → Lancelot migration cookbook"),
    ("docs/operator/runbook.md", "Operator runbook — 8 named failure modes + first actions"),
    ("docs/operator/failure-modes.md", "Single-page failure-modes table"),
    ("docs/operator/dashboards/lancelot-red.json", "Grafana RED dashboard (importable JSON)"),
    ("docs/plans/M5-PLAN-PROVE-VS-PEERS-AND-ADOPTABLE.md", "M5 milestone plan"),
    ("docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md", "M6 milestone plan"),
    ("docs/plans/M7-PLAN-DISTRIBUTED-PLUMBING-AND-MCP.md", "M7 milestone plan"),
    ("docs/peer-source-recheck/M5-peers.md", "M5 peer source recheck (Robyn / Blazeio / FastAPI)"),
    ("docs/peer-source-recheck/M6-ROBYN-PERF.md", "M6 Robyn perf deep-dive"),
    ("docs/peer-source-recheck/M6-BLAZEIO-PERF.md", "M6 Blazeio perf deep-dive"),
    ("benchmarks/README.md", "Benchmarks methodology + honest perf envelope"),
    ("benchmarks/results/M5/ANALYSIS.md", "M5 lab body bench analysis"),
    ("benchmarks/results/M6/ANALYSIS.md", "M6 lab body bench analysis"),
]

# Per-file return cap (bytes). Per Dex Horthy: MCP tools returning big
# JSON blobs flood the context. 32 KB is enough for any single Lancelot
# doc; anything bigger is a sign the doc should be split.
MAX_FILE_BYTES = 32 * 1024


def _doc_exists(rel_path: str) -> bool:
    p = REPO_ROOT / rel_path
    return p.is_file()


def _read_doc(rel_path: str) -> str:
    """Read a doc file with the per-file size cap. Returns the content
    truncated to MAX_FILE_BYTES with a marker if cut."""
    p = REPO_ROOT / rel_path
    if not p.is_file():
        return f"ERROR: doc not found: {rel_path}"
    raw = p.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        truncated = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        return (f"{truncated}\n\n"
                f"[truncated at {MAX_FILE_BYTES} bytes; original is "
                f"{len(raw)} bytes — read more with `lancelot_docs_get` "
                f"+ offsets if needed (not yet wired)]")
    return raw.decode("utf-8", errors="replace")


def _search_docs(query: str, limit: int = 5) -> list[dict]:
    """Grep-style search across the doc list. Returns matching SNIPPETS
    only (not full files) per the MCP-tool minimal-return discipline."""
    rx = re.compile(re.escape(query), re.IGNORECASE)
    results: list[dict] = []
    for rel_path, title in DOC_PATHS:
        if not _doc_exists(rel_path):
            continue
        try:
            content = (REPO_ROOT / rel_path).read_text(
                encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        match_count = 0
        for m in rx.finditer(content):
            if match_count >= 3:  # cap per-file matches to 3
                break
            start = max(0, m.start() - 80)
            end = min(len(content), m.end() + 200)
            snippet = content[start:end].strip()
            results.append({
                "path": rel_path,
                "title": title,
                "snippet": snippet,
            })
            match_count += 1
        if len(results) >= limit:
            break
    return results[:limit]


# ----- MCP server wiring -------------------------------------------------

mcp = FastMCP("lancelot-docs")


@mcp.tool()
def lancelot_docs_list() -> list[dict]:
    """List Lancelot's documentation files exposed via this MCP server.
    Use lancelot_docs_get(path) to read one, or lancelot_docs_search(query)
    to search across all of them."""
    return [
        {"path": rel, "title": title, "exists": _doc_exists(rel)}
        for rel, title in DOC_PATHS
    ]


@mcp.tool()
def lancelot_docs_get(path: str) -> str:
    """Read one Lancelot doc file by relative path. Use lancelot_docs_list
    to see what's available. Returns the full content (capped at 32 KB
    per MCP minimal-return discipline)."""
    # Allow-list check — only paths registered in DOC_PATHS are readable.
    allowed = {p for p, _ in DOC_PATHS}
    if path not in allowed:
        return (f"ERROR: path not in allowlist. Use lancelot_docs_list to see "
                f"the {len(DOC_PATHS)} available paths.")
    return _read_doc(path)


@mcp.tool()
def lancelot_docs_search(query: str, limit: int = 5) -> list[dict]:
    """Grep-style case-insensitive search across all Lancelot docs.
    Returns up to `limit` matching snippets (≤3 per file). Snippets are
    ±80/+200 chars around each match — NOT full files (per Dex Horthy's
    MCP-tool minimal-return rule: tools returning big JSON blobs flood
    the agent's context window)."""
    if not query.strip():
        return [{"error": "query must not be empty"}]
    return _search_docs(query, limit=limit)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
