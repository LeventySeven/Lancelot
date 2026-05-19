"""Deterministic per-file classifier for the vendored pinned FastAPI
corpus (harness-design §5). NO test is executed or parsed for behavior —
only its imports/literals are read via `ast`. The tag drives the
manifests; the anti-cheat (`--check`, invoked by ratchet.py) makes it
impossible to park a behavioral test in the EXCLUDE bin.

Order of the rule (first match wins):
  1. under tests/benchmarks/                 -> EXCLUDE (perf, not conformance)
  2. imports a FASTAPI_PRIVATE symbol         -> EXCLUDE  (Class C; reason=symbol)
  3. asserts a byte-exact /openapi.json|/docs|/redoc snapshot -> A2 (xfail)
  4. constructs Request/WebSocket in-test, or introspects app.router/
     custom APIRoute (reaches past the HTTP boundary)         -> CLASS B
  5. else                                                     -> CLASS A
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
CORPUS = ROOT / "vendor" / "fastapi" / "tests"

# Importing any of these = the test asserts a FastAPI INTERNAL (reflection
# helper / pydantic-v1 shim / private routing), not wire behavior. Class C.
FASTAPI_PRIVATE = (
    "fastapi.dependencies.utils", "fastapi.dependencies.models",
    "fastapi._compat", "fastapi.utils", "fastapi.concurrency",
    "fastapi.openapi.utils", "fastapi.openapi.constants", "fastapi.params",
    "fastapi.datastructures", "fastapi.encoders", "fastapi.exception_handlers",
    "fastapi.routing", "fastapi.applications", "fastapi.dependencies",
    "fastapi.openapi.docs", "fastapi.openapi.models", "fastapi.cli",
)
SNAPSHOT_PATHS = ("/openapi.json", "/docs", "/redoc")
# Past-the-HTTP-boundary symbols (constructed in-test / introspected).
BOUNDARY = ("Request", "WebSocket", "APIRoute", "APIWebSocketRoute")


def _imports(tree: ast.AST):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
            for a in n.names:
                out.add(f"{n.module}.{a.name}")
    return out


def _str_consts(tree: ast.AST):
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def classify_file(path: pathlib.Path) -> tuple[str, str]:
    rel = path.relative_to(CORPUS.parent).as_posix()
    if "tests/benchmarks/" in rel or rel.endswith("/benchmarks"):
        return "EXCLUDE", "tests/benchmarks/* — performance, not conformance"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        # py3.x-version-gated tutorial variants etc. — not Class A.
        return "EXCLUDE", "unparseable on this interpreter (version-gated)"
    imps = _imports(tree)
    for priv in FASTAPI_PRIVATE:
        if any(i == priv or i.startswith(priv + ".") for i in imps):
            return "EXCLUDE", f"imports FastAPI-private `{priv}` (Class C: " \
                              f"asserts a framework internal, not wire bytes)"
    if any(i == "inline_snapshot" or i.startswith("inline_snapshot.")
           for i in imps):
        # `inline_snapshot` IS the byte-snapshot mechanism — definitionally
        # a Lane-A2 (OpenAPI/HTML byte-exact) test.
        return "A2", "uses inline_snapshot (byte-exact snapshot; Lane A2, " \
                     "semantic-equiv only at v0)"
    consts = _str_consts(tree)
    if any(s in consts for s in SNAPSHOT_PATHS):
        # Asserts the literal /openapi.json|/docs|/redoc surface → snapshot.
        return "A2", "byte-exact OpenAPI/Swagger/ReDoc snapshot " \
                     "(semantic-equiv only at v0; Lane A2 xfail)"
    boundary_imp = any(
        i in ("fastapi.Request", "fastapi.WebSocket", "fastapi.routing.APIRoute",
              "starlette.requests.Request", "starlette.websockets.WebSocket")
        or i.endswith(".APIRoute") for i in imps)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if boundary_imp or ("APIRoute" in names and "fastapi" in str(imps)):
        return "B", "reaches past the HTTP boundary (constructs Request/" \
                    "WebSocket / custom APIRoute) — compat-shim lane"
    return "A", "run unmodified through the swap"


def classify_all():
    res = {}
    for p in sorted(CORPUS.rglob("*.py")):
        if p.name == "__init__.py":
            continue
        tag, reason = classify_file(p)
        res[p.relative_to(CORPUS.parent).as_posix()] = (tag, reason)
    return res


def _check() -> int:
    """Anti-cheat: every file in exclude.toml MUST be independently
    EXCLUDE-tagged by this classifier. You cannot park a behavioral
    (Class A/B) test in the exclude bin to inflate the ratio."""
    import tomllib

    exc = ROOT / "exclude.toml"
    if not exc.exists():
        print("exclude.toml missing"); return 1
    listed = tomllib.loads(exc.read_text()).get("files", {})
    res = classify_all()
    bad = []
    for f in listed:
        tag = res.get(f, ("MISSING", ""))[0]
        if tag != "EXCLUDE":
            bad.append((f, tag))
    if bad:
        print("ANTI-CHEAT FAIL: exclude.toml lists non-EXCLUDE files:")
        for f, t in bad:
            print(f"  {f}  -> classifier says {t}")
        return 1
    print(f"anti-cheat OK: all {len(listed)} excluded files import a "
          f"private symbol / are benchmarks")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(_check())
    if "--emit" in sys.argv:
        res = classify_all()
        from collections import Counter
        c = Counter(t for t, _ in res.values())
        exc_lines, x_lines = [], []
        for f, (t, r) in sorted(res.items()):
            if t == "EXCLUDE":
                exc_lines.append(f'"{f}" = "{r}"')
            elif t == "A2":
                x_lines.append(f'"{f}" = "{r}"')
        (ROOT / "exclude.toml").write_text(
            "# GENERATED by classify.py --emit (append-only; anti-cheat: "
            "ratchet.py runs --check).\n[files]\n" + "\n".join(exc_lines)
            + "\n")
        (ROOT / "xfail_snapshot.toml").write_text(
            "# GENERATED by classify.py --emit. Lane A2: byte-exact "
            "OpenAPI/HTML snapshots (semantic-equiv only at v0).\n[files]\n"
            + "\n".join(x_lines) + "\n")
        print(dict(c))
