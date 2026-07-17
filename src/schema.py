"""Dataset schema, tokenization, unified-diff and grounded reasoning builders.

Reasoning is generated ONLY from (a) the mutation we injected (we know it exactly)
and (b) captured verifier output (real dotnet build/test). It never invents APIs,
files, or outcomes.
"""
import os, re, difflib
import pyarrow as pa

PARQUET_SCHEMA = pa.schema([
    ("task_id", pa.string()), ("split", pa.string()), ("repo_id", pa.string()),
    ("repo_url", pa.string()), ("commit_sha", pa.string()), ("license", pa.string()),
    ("category", pa.string()), ("target_framework", pa.string()), ("project_path", pa.string()),
    ("changed_files", pa.list_(pa.string())), ("prompt", pa.string()),
    ("buggy_context", pa.string()), ("compiler_output", pa.string()),
    ("test_output", pa.string()), ("gold_patch", pa.string()),
    ("gold_fixed_context", pa.string()), ("reasoning", pa.string()),
    ("fast_response", pa.string()), ("verify_response", pa.string()),
    ("verifier_command", pa.string()),
    ("verifier_exit_code_before", pa.int32()), ("verifier_exit_code_after", pa.int32()),
    ("tests_before", pa.string()), ("tests_after", pa.string()),
    ("context_token_count", pa.int32()), ("response_token_count", pa.int32()),
    ("mutation_family", pa.string()), ("source_hash", pa.string()),
])

SCHEMA_FIELDS = [
    "task_id", "split", "repo_id", "repo_url", "commit_sha", "license",
    "category", "target_framework", "project_path", "changed_files", "prompt",
    "buggy_context", "compiler_output", "test_output", "gold_patch",
    "gold_fixed_context", "reasoning", "fast_response", "verify_response",
    "verifier_command", "verifier_exit_code_before", "verifier_exit_code_after",
    "tests_before", "tests_after", "context_token_count", "response_token_count",
    "mutation_family", "source_hash",
]
assert len(SCHEMA_FIELDS) == 28, len(SCHEMA_FIELDS)   # 28 data fields; split added separately

def _cat(family):
    import config as _C
    return _C.CATEGORY_OF.get(family, "compile_type_null_api")

_TOK = None
_TOK_TRIED = False
def tokenizer():
    global _TOK, _TOK_TRIED
    if not _TOK_TRIED:
        _TOK_TRIED = True
        try:
            from transformers import AutoTokenizer
            _TOK = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-Coder-1.5B", trust_remote_code=True)
        except Exception:
            _TOK = None   # transformers unavailable -> heuristic; finalize recomputes exactly
    return _TOK

def ntok(s):
    if not s:
        return 0
    t = tokenizer()
    if t is None:
        return max(1, len(s) // 4)
    try:
        return len(t(s).input_ids)
    except Exception:
        return max(1, len(s) // 4)

def unified_diff(buggy_text, fixed_text, path):
    a = buggy_text.splitlines(keepends=True)
    b = fixed_text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile="a/" + path, tofile="b/" + path))

_ERR = re.compile(r"(?m)^[^\s].*?:\s*error\s+(CS\d+):.*$", )  # compiler error lines
def _compiler_errors(out):
    return _ERR.findall(out or "")[:6]

def _test_fail_lines(out):
    lines = []
    for ln in (out or "").splitlines():
        l = ln.lower()
        if any(k in l for k in ("failed!", "failed:", "failed ", "error ", "[xunit]", "assert", "expected", "but was", "  -  failed", "failed:")):
            lines.append(ln.strip())
        if len(lines) >= 8:
            break
    return lines

def make_reasoning(family, desc, file_rel, line, broken_via, broken_out, broken_rc):
    """Grounded <diagnosis>/<plan>/<patch>-style reasoning is assembled by build.py
    (it needs the diff). Here we provide the diagnosis+plan text only."""
    if broken_via == "build":
        errs = _compiler_errors(broken_out)
        evidence = ("compiler errors: " + "; ".join(errs)) if errs else "dotnet build failed"
        diagnosis = (f"`{file_rel}:{line}` fails to compile under `dotnet build` "
                     f"(exit {broken_rc}; {evidence}). Root cause: the source was changed "
                     f"({desc}), introducing the error.")
    else:
        fl = _test_fail_lines(broken_out)
        evidence = "; ".join(fl) if fl else "dotnet test reported failures"
        diagnosis = (f"`dotnet test` fails (exit {broken_rc}; {evidence}). Root cause: "
                     f"the behavior of `{file_rel}:{line}` was changed ({desc}), breaking "
                     f"a previously-passing test.")
    plan = ("1. Revert the changed code to the original, passing implementation.\n"
            "2. Rebuild and rerun the affected tests to confirm green.")
    return diagnosis, plan

def make_prompt(repo_id, category, buggy_context, compiler_output, test_output, changed_files, path):
    co = compiler_output.strip() or "(build succeeded)"
    to = test_output.strip() or "(no tests / build-only check)"
    return (
        f"Repository: {repo_id}\n"
        f"File: {path}\n"
        f"Repair category: {category}\n\n"
        f"The following C# file has a single localized defect. Diagnose it using the "
        f"verifier evidence, then produce a minimal unified diff that repairs it so the "
        f"project compiles and all relevant tests pass.\n\n"
        f"--- COMPILER OUTPUT ---\n{co}\n\n"
        f"--- TEST OUTPUT ---\n{to}\n\n"
        f"--- BUGGY FILE ({path}) ---\n{buggy_context}\n\n"
        f"Respond with <diagnosis>, <plan>, then <patch> (unified diff only).\n"
    )
