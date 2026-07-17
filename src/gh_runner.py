"""GitHub Actions batch runner: time-bounded + resumable.

Each workflow run reads manifest.json (next_idx) from the HF dataset repo,
processes a bounded window of candidates (until TIME_BUDGET_S or BATCH_REPOS),
flushes raw/shard-*.parquet + updated manifest back, then exits. Repeated
scheduled/dispatched runs walk the whole candidate list. All cloning/building/
testing runs on an isolated GitHub runner -- never on a user's machine.
"""
import os, sys, json, time, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import build as B
import schema as S
import pyarrow as pa
import pyarrow.parquet as pq
from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download

api = HfApi(token=C.TOKEN)
SCHEMA = S.PARQUET_SCHEMA
TIME_BUDGET = float(os.environ.get("TIME_BUDGET_S", "3000"))
BATCH_REPOS = int(os.environ.get("BATCH_REPOS", "400"))


def log(*a):
    print(*a, flush=True)


def load_candidates():
    p = os.environ.get("CPT_CANDIDATES",
                       os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "candidates.txt")))
    out = []
    for ln in open(p, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        x = ln.split("\t")
        out.append((x[0], x[1] if len(x) > 1 else "mit"))
    return out


def load_manifest():
    try:
        p = hf_hub_download(C.HF_REPO, "manifest.json", repo_type="dataset", token=C.TOKEN)
        return json.load(open(p, encoding="utf-8"))
    except Exception as e:
        log("no manifest, fresh start:", repr(e)[:120])
        return {"next_idx": 0, "processed": {}, "shards": [],
                "counts": {"tasks": 0, "repos_built": 0, "repos_processed": 0}}


def commit(ops, msg):
    for attempt in range(8):
        try:
            api.create_commit(repo_id=C.HF_REPO, repo_type="dataset", operations=ops,
                              commit_message=msg, token=C.TOKEN)
            return True
        except Exception as e:
            s = str(e)
            if "429" in s or "rate" in s.lower():
                log("  429, sleep 300s"); time.sleep(300); continue
            log("  commit err:", repr(e)[:200]); time.sleep(20)
    return False


def save_manifest(man, extra_ops=None):
    os.makedirs(C.SCRATCH, exist_ok=True)
    tmp = os.path.join(C.SCRATCH, "manifest.json")
    json.dump(man, open(tmp, "w"), indent=2)
    ops = [CommitOperationAdd(path_in_repo="manifest.json", path_or_fileobj=tmp)] + list(extra_ops or [])
    return commit(ops, "checkpoint: manifest" + (" + shard" if extra_ops else ""))


def flush_shard(buf, man, shard_idx):
    rows = []
    for t in buf:
        t = dict(t)
        t.pop("_broken_via", None)
        for k in ("verifier_exit_code_before", "verifier_exit_code_after",
                  "context_token_count", "response_token_count"):
            t[k] = int(t.get(k) or 0)
        rows.append(t)
    tbl = pa.Table.from_pylist(rows, schema=SCHEMA)
    path = os.path.join(C.SCRATCH, f"raw_{shard_idx:05d}.parquet")
    pq.write_table(tbl, path, compression="ZSTD", compression_level=9)
    repo_path = f"raw/shard-{shard_idx:05d}.parquet"
    op = CommitOperationAdd(path_in_repo=repo_path, path_or_fileobj=path)
    man["shards"].append({"file": repo_path, "rows": len(rows)})
    man["counts"]["tasks"] = man["counts"].get("tasks", 0) + len(rows)
    ok = save_manifest(man, extra_ops=[op])
    if ok:
        try:
            os.remove(path)
        except OSError:
            pass
        log(f"  >> flushed {repo_path} ({len(rows)} rows) | total tasks={man['counts']['tasks']}")
    return ok


def main():
    cands = load_candidates()
    man = load_manifest()
    processed = set(man.get("processed", {}).keys())
    buf = []
    shard_idx = len(man.get("shards", []))
    idx = man.get("next_idx", 0)
    while idx < len(cands) and cands[idx][0] in processed:
        idx += 1
    t0 = time.time()
    repos_done = 0
    log(f"=== gh_runner start | {len(cands)} cands | idx={idx} | "
        f"budget={TIME_BUDGET:.0f}s | cap={BATCH_REPOS} | disk={B.free_gb():.1f}G ===")

    while idx < len(cands):
        if time.time() - t0 > TIME_BUDGET:
            log("time budget reached -> stopping run"); break
        if repos_done >= BATCH_REPOS:
            log("batch repo cap reached"); break
        if B.free_gb() < C.MIN_FREE_DISK_GB:
            shutil.rmtree(C.NUGET, ignore_errors=True)
            os.makedirs(C.NUGET, exist_ok=True)
            if B.free_gb() < C.MIN_FREE_DISK_GB:
                log("low disk -> stopping run"); break
        repo, lic = cands[idx]
        url = "https://github.com/" + repo
        try:
            tasks, stat = B.process_repo(repo, url, lic, max_tasks=C.MAX_MUTATIONS_PER_REPO)
        except Exception as e:
            tasks, stat = [], {"reason": "exception: " + repr(e)[:200]}
        processed.add(repo)
        man.setdefault("processed", {})[repo] = (stat.get("reason")
                                                 or ("ok" if stat.get("ok") else "unknown"))
        man["counts"]["repos_processed"] = man["counts"].get("repos_processed", 0) + 1
        if stat.get("ok"):
            man["counts"]["repos_built"] = man["counts"].get("repos_built", 0) + 1
        buf += tasks
        repos_done += 1
        idx += 1
        man["next_idx"] = idx
        log(f"[{idx:5d}/{len(cands)}] {repo:42s} kept={stat.get('kept',0):3d} "
            f"reason={stat.get('reason')} buf={len(buf)} t={time.time()-t0:.0f}s")
        if len(buf) >= C.SHARD_ROWS or repos_done % 20 == 0:
            if buf:
                if flush_shard(buf, man, shard_idx):
                    buf = []
                    shard_idx += 1
            else:
                save_manifest(man)
    if buf:
        flush_shard(buf, man, shard_idx)
    save_manifest(man)
    log(f"=== gh_runner run done | +{repos_done} repos | next_idx={man['next_idx']} | "
        f"total tasks={man['counts'].get('tasks')} | built={man['counts'].get('repos_built')} ===")


if __name__ == "__main__":
    main()
