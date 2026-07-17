"""Deterministic build/test harness for generating verified C# repair tasks.

Pipeline per repo:
  clone (shallow) @ HEAD -> record SHA
  dotnet restore (online, into offline cache)
  baseline: build MUST pass; if tests exist, test MUST pass + hermetic + not flaky
  for each sampled mutation point:
     write mutated file  -> build  (capture compiler_output)
                         -> if build ok and tests: test (capture test_output)
     broken must FAIL deterministically (re-run once)
     restore original (gold) -> build MUST pass -> test MUST pass
     => keep task; else discard
  delete clone (stream; bounded disk)
"""
import os, sys, subprocess, shutil, glob, re, time, hashlib, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import schema as S

DOTNET = "dotnet"
MUTATOR_DLL = C.MUTATOR_DLL
COMMON_ENV = {
    **os.environ,
    "NUGET_PACKAGES": C.NUGET,
    "DOTNET_ROLL_FORWARD": "Major",
    "DOTNET_NOLOGO": "true",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "true",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "true",
    "MSBUILDDISABLENODEREUSE": "1",   # avoid msbuild server holding file locks
    "MSBUILDUSERGLOBALPATH": "1",
    "GIT_TERMINAL_PROMPT": "0",       # fail instead of prompting -> never hang unattended
    "GCM_INTERACTIVE": "Never",       # git-credential-manager: no GUI prompt
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
}

SUSPICIOUS_NET = re.compile(
    r"(HttpClient|WebRequest|WebClient|System\.Net\.Sockets|new Socket|TcpClient|UdpClient|"
    r"HttpListener|Dns\.|FtpWebR|Process\.Start|ChatGPT|localhost|127\.0\.0\.1)")
TEST_PROJ_MARKERS = ("xunit", "nunit", "microsoft.net.test.sdk", "mstest")


def run(cmd, cwd=None, timeout=300, env=None):
    e = dict(COMMON_ENV)
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as te:
        o = (te.stdout or "")
        if isinstance(o, bytes):
            o = o.decode("utf-8", "replace")
        e2 = (te.stderr or "")
        if isinstance(e2, bytes):
            e2 = e2.decode("utf-8", "replace")
        return 124, (o + e2 + f"\n[TIMEOUT after {timeout}s]\n")


def clone(repo_url, dst, timeout=C.CLONE_TIMEOUT):
    if os.path.exists(dst):
        _rmtree_hard(dst)
    rc, out = run(["git", "-c", "credential.helper=", "-c", "core.askpass=",
                   "clone", "--depth=1", "--no-tags", repo_url, dst], timeout=timeout)
    if rc != 0:
        return None, out
    rc, sha = run(["git", "rev-parse", "HEAD"], cwd=dst, timeout=30)
    return sha.strip(), out


def find_solution(root):
    slns = glob.glob(os.path.join(root, "**", "*.sln"), recursive=True)
    top = [s for s in slns if os.path.dirname(s) == root]
    return (top or slns)[0] if (top or slns) else None


def find_csprojs(root):
    return glob.glob(os.path.join(root, "**", "*.csproj"), recursive=True)


def repo_has_tests(root):
    for cs in find_csprojs(root):
        try:
            t = open(cs, encoding="utf-8", errors="ignore").read().lower()
        except OSError:
            t = ""
        if any(k in t for k in TEST_PROJ_MARKERS):
            return True
    return False


def find_test_projects(root):
    """Return test .csproj paths. Building/testing THESE (not the .sln) avoids
    building old .NET Framework TFMs that fail on Linux, and the library is built
    in the (modern) TFM the test project consumes."""
    out = []
    for cs in find_csprojs(root):
        try:
            t = open(cs, encoding="utf-8", errors="ignore").read().lower()
        except OSError:
            continue
        if any(k in t for k in TEST_PROJ_MARKERS):
            out.append(cs)
    return out


def target_framework(root):
    for cs in find_csprojs(root):
        try:
            t = open(cs, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        m = re.search(r"<TargetFrameworks?>(.*?)</TargetFrameworks?>", t, re.I | re.S)
        if m:
            return m.group(1).strip()
    return "unknown"


_MODERN_PREFIX = ("net5.", "net6.", "net7.", "net8.", "net9.", "net10.",
                  "netcoreapp", "netstandard")


def repo_tfms(root):
    tfms = set()
    for cs in find_csprojs(root):
        try:
            t = open(cs, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for m in re.findall(r"<TargetFrameworks?>(.*?)</TargetFrameworks?>", t, re.I | re.S):
            for tok in re.split(r"[;\s]+", m):
                tok = tok.strip().lower()
                if tok:
                    tfms.add(tok)
    return tfms


def has_modern_target(root):
    tfms = repo_tfms(root)
    if not tfms:
        return True  # no explicit TFM parseable -> let baseline decide
    return any(any(t.startswith(p) for p in _MODERN_PREFIX) for t in tfms)


# ---------- license integrity: read the actual LICENSE file ----------
_EXCLUDE_MARKERS = ("gnu affero", "gnu general public", "gnu lesser", "lgpl",
                    "mozilla public license", "eclipse public license", "gpl")


def detect_license(root):
    text = ""
    for f in sorted(os.listdir(root)):
        fl = f.lower()
        if fl.startswith("license") or fl.startswith("copying") or fl == "licence":
            try:
                text += open(os.path.join(root, f), encoding="utf-8", errors="ignore").read() + "\n"
            except OSError:
                pass
            if len(text) > 8000:
                break
    if not text:
        return None
    low = text.lower()
    if any(m in low for m in _EXCLUDE_MARKERS) and "this file is part of" not in low[:200]:
        # be careful: an MIT LICENSE next to an LGPL NOTICE; only flag if header itself is copyleft
        if not ("permission is hereby granted, free of charge" in low):
            return "gpl-family"
    if "apache license" in low and ("version 2.0" in low or "2.0" in low[:400]):
        return "apache-2.0"
    if "permission is hereby granted, free of charge" in low:
        return "mit"
    if "redistribution and use in source and binary forms" in low:
        return "bsd-3-clause" if "endorse" in low or "neither the name" in low else "bsd-2-clause"
    if "isc license" in low or ("permission to use, copy, modify" in low and "isc" in low):
        return "isc"
    if low.strip().startswith("this is free and unencumbered software") or "unlicense" in low[:60]:
        return "unlicense"
    if "creative commons zero" in low or "cc0 1.0" in low:
        return "cc0-1.0"
    if "microsoft public license" in low or "ms-pl" in low:
        return "ms-pl"
    return "unknown"


def dir_size_mb(path, cap_mb=300):
    total = 0
    for r, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
        if total > cap_mb * 1024 * 1024:
            break
    return total / (1024 * 1024)


MAX_CLONE_MB = 150


def is_hermetic(root):
    for f in glob.glob(os.path.join(root, "**", "*.cs"), recursive=True):
        low = f.replace("\\", "/").lower()
        if "test" not in low:
            continue
        try:
            t = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if SUSPICIOUS_NET.search(t):
            return False
    return True


def restore(target, root):
    return run([DOTNET, "restore", target], cwd=root, timeout=C.RESTORE_TIMEOUT)


def build(target, root):
    return run([DOTNET, "build", target, "-c", "Release", "--no-restore",
                "-p:TreatWarningsAsErrors=false", "-clp:ErrorsOnly", "-v", "m"],
               cwd=root, timeout=C.BUILD_TIMEOUT)


def test(target, root):
    return run([DOTNET, "test", target, "-c", "Release", "--no-restore", "--no-build",
                "-p:TreatWarningsAsErrors=false", "--nologo"], cwd=root, timeout=C.TEST_TIMEOUT)


def build_targets(targets, root):
    out = ""
    for t in targets:
        r, o = build(t, root)
        out += o
        if r != 0:
            return r, out
    return 0, out


def test_targets(targets, root):
    out = ""
    for t in targets:
        r, o = test(t, root)
        out += o
        if r != 0:
            return r, out
    return 0, out


def _kill_compilers():
    """Kill the Roslyn compiler server / msbuild so they release file locks on
    Windows (single-threaded use only -- safe to kill globally here)."""
    for exe in ("VBCSCompiler.exe", "MSBuild.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", exe],
                           capture_output=True, timeout=15)
        except Exception:
            pass


def _rmtree_hard(dst):
    for _ in range(8):
        try:
            shutil.rmtree(dst)
            return
        except OSError:
            _kill_compilers()
            time.sleep(2)
    shutil.rmtree(dst, ignore_errors=True)


def free_gb(path=None):
    """Cross-platform free-disk check (GB)."""
    return shutil.disk_usage(path or C.WORK).free / (1024 ** 3)


def baseline(root):
    sln = find_solution(root)
    if not sln:
        projs = find_csprojs(root)
        if not projs:
            return {"ok": False, "reason": "no_sln_or_csproj"}
        sln = projs[0]
    rc, out = restore(sln, root)
    if rc != 0:
        return {"ok": False, "reason": "restore_failed", "output": out[-3000:]}
    # Build/test the TEST projects specifically (not the .sln) so multi-targeted
    # libraries are built only in the modern TFM the tests consume -> no .NET
    # Framework TFM failures on Linux.
    test_projs = find_test_projects(root)
    targets = test_projs if test_projs else [sln]
    has_t = bool(test_projs)
    rc, bout = build_targets(targets, root)
    if rc != 0:
        return {"ok": False, "reason": "baseline_build_failed", "output": bout[-3000:]}
    info = {"ok": True, "sln": sln, "targets": targets, "has_tests": has_t,
            "test_out": "", "tests_before": ""}
    if not has_t:
        return info
    rc, tout = test_targets(targets, root)
    if rc != 0:
        return {"ok": False, "reason": "baseline_tests_fail", "output": tout[-3000:]}
    if not is_hermetic(root):
        return {"ok": False, "reason": "non_hermetic_tests", "output": ""}
    rc2, tout2 = test_targets(targets, root)        # flakiness check
    if rc2 != 0:
        return {"ok": False, "reason": "flaky_tests", "output": tout2[-2000:]}
    info["test_out"] = tout
    info["tests_before"] = _test_summary(tout)
    return info


def _test_summary(out):
    for ln in (out or "").splitlines():
        if "Passed!" in ln or "Passed:" in ln or "Failed!" in ln or "Total tests" in ln:
            return ln.strip()
    return "(tests passed at baseline)"


def candidate_source_files(root):
    out = []
    for f in glob.glob(os.path.join(root, "**", "*.cs"), recursive=True):
        low = f.replace("\\", "/").lower()
        b = os.path.basename(f).lower()
        if "/bin/" in low or "/obj/" in low:
            continue
        if "/test/" in low or "/tests/" in low or b.startswith("test") or "test" in b:
            continue
        if b.endswith(".g.cs") or b.endswith(".designer.cs") or ".generated." in b:
            continue
        if b in ("assemblyinfo.cs", "assemblyattributes.cs", "globalusings.cs"):
            continue
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        if sz < C.MIN_FILE_CHARS or sz > C.MAX_FILE_CHARS:
            continue
        out.append(f)
    return out


def _truncate(s, n, mode="tail"):
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[-n:] if mode == "tail" else s[:n]


def _norm(s):
    """Normalize line endings to LF so diffs are minimal (Roslyn emits CRLF on
    Windows-authored files; Python text-read gives LF -> spurious whole-file diffs)."""
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def verify_one(root, targets, file, family, index, has_tests, tests_before):
    """Apply one mutation, verify broken-fails + gold-passes. Returns task dict or None."""
    try:
        orig = _norm(open(file, encoding="utf-8-sig", errors="replace").read())
    except OSError:
        return None
    rel = os.path.relpath(file, root).replace("\\", "/")
    tmp = file + ".mut"

    rc, meta_out = run([DOTNET, MUTATOR_DLL, "apply", file, family, str(index), tmp], timeout=90)
    if rc != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass
        return None
    import json
    try:
        meta = json.loads(meta_out)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        return None
    if not meta.get("applied"):
        try: os.remove(tmp)
        except OSError: pass
        return None
    desc = meta.get("desc", family)
    line = int(meta.get("line", 0))
    mutated = _norm(meta["new_text"])
    try: os.remove(tmp)
    except OSError: pass

    task = None
    try:
        # --- broken state: write mutated file in place ---
        with open(file, "w", encoding="utf-8", newline="") as fh:
            fh.write(mutated)
        b_rc, b_out = build_targets(targets, root)
        compiler_output = ""
        test_output = ""
        broken_via = None
        broken_rc = b_rc
        if b_rc != 0:
            broken_via = "build"
            compiler_output = _truncate(b_out, 4000, "head")
        elif has_tests:
            t_rc, t_out = test_targets(targets, root)
            if t_rc != 0:
                broken_via = "test"
                test_output = _truncate(t_out, 8000, "tail")
                broken_rc = t_rc
        if broken_via is None:
            return None  # mutation had no observable effect -> not a valid task
        # determinism re-run
        r_rc, _ = (build_targets(targets, root) if broken_via == "build"
                   else test_targets(targets, root))
        if r_rc == 0:
            return None  # flaky / not deterministic
        # --- gold: restore original ---
        with open(file, "w", encoding="utf-8", newline="") as fh:
            fh.write(orig)
        g_rc, g_out = build_targets(targets, root)
        if g_rc != 0:
            return None  # gold doesn't compile -> reject (shouldn't happen vs baseline)
        tests_after = ""
        verify_after = 0
        if has_tests:
            gt_rc, gt_out = test_targets(targets, root)
            if gt_rc != 0:
                return None  # gold fails tests -> reject
            tests_after = _test_summary(gt_out)
        # --- success: assemble task ---
        gold_patch = S.unified_diff(mutated, orig, rel)
        diagnosis, plan = S.make_reasoning(family, desc, rel, line, broken_via,
                                           (compiler_output or test_output), broken_rc)
        reasoning = (f"<diagnosis>\n{diagnosis}\n</diagnosis>\n<plan>\n{plan}\n</plan>\n"
                     f"<patch>\n{gold_patch}</patch>")
        fast_response = f"<patch>\n{gold_patch}</patch>"
        prompt = S.make_prompt(None, S._cat(family), mutated, compiler_output,
                               test_output, [rel], rel)
        vcmd = (f"dotnet build {os.path.relpath(targets[0], root)} -c Release --no-restore"
                if broken_via == "build"
                else f"dotnet test {os.path.relpath(targets[0], root)} -c Release --no-restore")
        task = {
            "task_id": "",        # filled by caller
            "repo_id": "", "repo_url": "", "commit_sha": "", "license": "",
            "category": S._cat(family), "target_framework": "", "project_path": "",
            "changed_files": [rel],
            "prompt": prompt,
            "buggy_context": mutated,
            "compiler_output": compiler_output,
            "test_output": test_output,
            "gold_patch": gold_patch,
            "gold_fixed_context": orig,
            "reasoning": reasoning,
            "fast_response": fast_response,
            "verify_response": (f"gold: dotnet build exit 0"
                                + (f"; dotnet test exit 0 ({tests_after})" if has_tests else "")),
            "verifier_command": vcmd,
            "verifier_exit_code_before": int(broken_rc),
            "verifier_exit_code_after": 0,
            "tests_before": tests_before,
            "tests_after": tests_after,
            "mutation_family": family,
            "source_hash": hashlib.sha256(mutated.encode("utf-8")).hexdigest(),
            "_broken_via": broken_via,
        }
    finally:
        # always restore original so the next mutation starts clean
        try:
            with open(file, "w", encoding="utf-8", newline="") as fh:
                fh.write(orig)
        except OSError:
            pass
    return task


def split_for_repo(repo_id):
    h = hashlib.blake2b(repo_id.encode("utf-8"), digest_size=8).digest()
    x = int.from_bytes(h, "big") / float(1 << 64)
    acc = 0.0
    for name, r in C.SPLIT_RATIOS.items():
        acc += r
        if x < acc:
            return name
    return "train"


_WORK_COUNTER = [0]


def _sweep_work(max_age_min=20):
    """Remove stale work dirs (leaked by Windows file locks on a prior kill)."""
    now = time.time()
    try:
        for d in os.listdir(C.WORK):
            p = os.path.join(C.WORK, d)
            try:
                if os.path.isdir(p) and now - os.path.getmtime(p) > max_age_min * 60:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def process_repo(repo_id, repo_url, license_name, log=print, max_tasks=None):
    """Clone, baseline, sample+verify mutations. Returns (list_of_tasks, status_dict)."""
    _sweep_work()
    _WORK_COUNTER[0] += 1
    dst = os.path.join(C.WORK, f"r{_WORK_COUNTER[0]:06d}_" +
                       re.sub(r"[^A-Za-z0-9._-]", "_", repo_id) +
                       "_" + f"{random.randint(0, 0xffffff):06x}")
    sha, cerr = clone(repo_url, dst)
    if not sha:
        lines = [ln for ln in cerr.splitlines() if ln.strip()]
        last = lines[-1][:200] if lines else "unknown"
        return [], {"repo_id": repo_id, "ok": False,
                    "reason": "clone_failed: " + last}
    try:
        # --- license integrity from the actual LICENSE file ---
        det = detect_license(dst)
        if det is None:
            det = license_name
        if det in C.ALLOWED_LICENSES:
            license_name = det
        elif license_name in C.ALLOWED_LICENSES:
            pass  # trust annotation if file unreadable
        else:
            return [], {"repo_id": repo_id, "ok": False, "reason": "license_not_allowed",
                        "detected": det, "commit_sha": sha}
        # --- size guard ---
        if dir_size_mb(dst) > MAX_CLONE_MB:
            return [], {"repo_id": repo_id, "ok": False, "reason": "too_large", "commit_sha": sha}
        if not has_modern_target(dst):
            return [], {"repo_id": repo_id, "ok": False, "reason": "framework_only_target",
                        "commit_sha": sha, "tfms": sorted(repo_tfms(dst))[:6]}
        base = baseline(dst)
        if not base.get("ok"):
            return [], {"repo_id": repo_id, "ok": False, "reason": base.get("reason"),
                        "commit_sha": sha, "output": base.get("output", "")[-1500:]}
        sln = base["sln"]
        has_tests = base["has_tests"]
        tests_before = base.get("tests_before", "")
        tfw = target_framework(dst)
        files = candidate_source_files(dst)
        random.seed(hash(repo_id) & 0xFFFFFFFF)
        random.shuffle(files)
        tasks = []
        attempted = 0
        t_repo = time.time()
        for f in files:
            if (max_tasks and len(tasks) >= max_tasks) or time.time() - t_repo > C.PER_REPO_BUDGET_S:
                break
            rc, enum_out = run([DOTNET, MUTATOR_DLL, "enumerate", f], timeout=60)
            if rc != 0:
                continue
            import json
            try:
                pts = json.loads(enum_out)
            except Exception:
                continue
            # group by family, sample a few indices per family
            by_fam = {}
            for p in pts:
                by_fam.setdefault(p["family"], []).append(p)
            for fam, lst in by_fam.items():
                if (max_tasks and len(tasks) >= max_tasks) or time.time() - t_repo > C.PER_REPO_BUDGET_S:
                    break
                if fam == "compile" and not has_tests:
                    pass  # compile tasks valid even w/o tests
                # logic/async/linq/framework need tests to detect failures
                if fam != "compile" and not has_tests:
                    continue
                idxs = list(range(min(len(lst), 6)))  # up to 6 points/family/file
                random.shuffle(idxs)
                for idx in idxs[:3]:
                    if (max_tasks and len(tasks) >= max_tasks) or time.time() - t_repo > C.PER_REPO_BUDGET_S:
                        break
                    attempted += 1
                    t = verify_one(dst, base["targets"], f, fam, idx, has_tests, tests_before)
                    if t is not None:
                        t["task_id"] = f"{repo_id}::{fam}::{t['source_hash'][:10]}"
                        t["repo_id"] = repo_id
                        t["repo_url"] = repo_url
                        t["commit_sha"] = sha
                        t["license"] = license_name
                        t["target_framework"] = tfw
                        t["project_path"] = os.path.dirname(os.path.relpath(sln, dst)).replace("\\", "/")
                        t["split"] = split_for_repo(repo_id)
                        t["context_token_count"] = S.ntok(t["buggy_context"])
                        t["response_token_count"] = S.ntok(t["reasoning"])
                        tasks.append(t)
        return tasks, {"repo_id": repo_id, "ok": True, "commit_sha": sha,
                       "files": len(files), "has_tests": has_tests,
                       "attempted": attempted, "kept": len(tasks), "framework": tfw}
    finally:
        _rmtree_hard(dst)
