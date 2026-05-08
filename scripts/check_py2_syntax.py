#!/usr/bin/env python3
"""
Static guard: assert nobody snuck Py3-only syntax into pyfreerdp/.

This runs in CI to keep the source uniformly written in
Py2-syntax-compatible style. The package only ever runs on Python 3,
but we restrict the syntax to the subset that would also parse on 2.7.

Forbidden constructs:
  - f-strings ("f'...'", 'f"..."')
  - Type annotations on functions, parameters, or assignments
  - Walrus operator (:=)
  - Match statements (match ... :)
  - Keyword-only or positional-only parameter separators (*, /)

Run:
    python scripts/check_py2_syntax.py

Exit code 0 = clean, 1 = violations found.
"""
import ast
import os
import sys
import tokenize


PACKAGE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pyfreerdp")


def detect_fstrings(path, src):
    """Find f-string literals via the tokenize stream (more reliable than regex)."""
    out = []
    try:
        with open(path, "rb") as fh:
            tokens = list(tokenize.tokenize(fh.readline))
    except Exception:
        return out
    for tok in tokens:
        # Python 3.12+ exposes FSTRING_START / FSTRING_MIDDLE / FSTRING_END.
        if tok.type in (getattr(tokenize, "FSTRING_START", -1),):
            out.append((tok.start[0], "fstring", tok.string))
        elif tok.type == tokenize.STRING and tok.string and (
                tok.string[:2] in ('f"', "f'") or
                tok.string[:3] in ('f"""', "f'''") or
                tok.string[:2] in ('rf', 'fR', 'Rf', 'fr') or
                tok.string[:3] in ('rf"', "rf'", 'fr"', "fr'")):
            out.append((tok.start[0], "fstring", tok.string[:30]))
    return out


def detect_ast_problems(src):
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            out.append((node.lineno, "annotated-assign", ""))
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            if node.returns is not None:
                out.append((node.lineno, "return-annotation", node.name))
            args = node.args
            for arg in list(args.args) + list(args.kwonlyargs) + list(
                    getattr(args, "posonlyargs", [])):
                if arg.annotation is not None:
                    out.append((node.lineno, "param-annotation",
                                node.name + ":" + arg.arg))
            if args.kwonlyargs:
                out.append((node.lineno, "kw-only-arg", node.name))
            if getattr(args, "posonlyargs", None):
                out.append((node.lineno, "pos-only-arg", node.name))
        if isinstance(node, ast.NamedExpr):    # walrus :=
            out.append((node.lineno, "walrus", ""))
        # Match statement: ast.Match exists in 3.10+
        Match = getattr(ast, "Match", None)
        if Match is not None and isinstance(node, Match):
            out.append((node.lineno, "match-stmt", ""))
    return out


def main():
    failures = []
    for root, _, files in os.walk(PACKAGE_ROOT):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(root, f)
            with open(p, "r", encoding="utf-8") as fh:
                src = fh.read()
            problems = []
            problems += detect_fstrings(p, src)
            problems += detect_ast_problems(src)
            if problems:
                failures.append((p, problems))

    if not failures:
        print("OK: no Py3-only syntax found in {0}".format(PACKAGE_ROOT))
        return 0

    print("FAIL: Py3-only syntax found:")
    for path, ps in failures:
        print("\n" + path)
        for ln, kind, snippet in ps:
            print("  L{0}: {1}: {2}".format(ln, kind, snippet[:80]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
