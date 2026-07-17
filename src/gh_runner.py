"""GitHub Actions batch runner: sharded + time-bounded + resumable.

NSHARDS jobs run in parallel (workflow matrix), each owning the candidates whose
global index % NSHARDS == SHARD -- disjoint slices, so no two jobs touch the same
repo and there is NO shared manifest to race on. Each shard writes its own
manifest_s<SHARD>.json and raw/s<SHARD>-*.parquet. finalize.py aggregates all of
them. All cloning/building/testing runs on the isolated GitHub runner.
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
SHARD = int(os.environ.get("SHARD", "0"))
NSHARDS = int(os.environ.get("NSHARDS", "1"))
TIME_BUDGET = float(os.environ.get("TIME_BUDGET_S", "2700"))
BATCH_REPOS = int(os.environ.get("BATCH_REPOS", "9999"))
SHARDED = NSHARDS > 1
MAN_NAME = (f"manifest_s{SHARD}.json" if SHARDED else "manifest.json")


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


def my_slice(cands):
    return [(i, c) for i, c in enumerate(cands) if i % NSHARDS == SHARD]


def load_manifest():
    try:
        p = hf_hub_download(C.HF_REPO, MAN_NAME, repo_type="dataset", token=C.TOKEN)
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {"processed": {}, "shards": [],
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
    tmp = os.path.join(C.SCRATCH, MAN_NAME)
    json.dump(man, open(tmp, "w"), indent=2)
    ops = [CommitOperationAdd(path_in_repo=MAN_NAME, path_or_fileobj=tmp)] + list(extra_ops or [])
    return commit(ops, f"[shard {SHARD}] checkpoint" + (" + shard" if extra_ops else ""))


def flush_shard(buf, man, sidx):
    rows = []
    for t in buf:
        t = dict(t)
        t.pop("_broken_via", None)
        for k in ("verifier_exit_code_before", "verifier_exit_code_after",
                  "context_token_count", "response_token_count"):
            t[k] = int(t.get(k) or 0)
        rows.append(t)
    tbl = pa.Table.from_pylist(rows, schema=SCHEMA)
    path = os.path.join(C.SCRATCH, f"raw_{SHARD}_{sidx:05d}.parquet")
    pq.write_table(tbl, path, compression="ZSTD", compression_level=9)
    rp = (f"raw/s{SHARD:02d}-{sidx:05d}.parquet" if SHARDED else f"raw/shard-{sidx:05d}.parquet")
    op = CommitOperationAdd(path_in_repo=rp, path_or_fileobj=path)
    man["shards"].append({"file": rp, "rows": len(rows)})
    man["counts"]["tasks"] = man["counts"].get("tasks", 0) + len(rows)
    ok = save_manifest(man, extra_ops=[op])
    if ok:
        try:
            os.remove(path)
        except OSError:
            pass
        log(f"  >> flushed {rp} ({len(rows)} rows) | shard tasks={man['counts']['tasks']}")
    return ok


def main():
    cands = load_candidates()
    sl = my_slice(cands)
    man = load_manifest()
    processed = set(man.get("processed", {}).keys())
    buf = []
    sidx = len(man.get("shards", []))
    t0 = time.time()
    repos_done = 0
    log(f"=== shard {SHARD}/{NSHARDS} | slice={len(sl)} | budget={TIME_BUDGET:.0f}s "
        f"| cap={BATCH_REPOS} | disk={B.free_gb():.1f}G ===")
    for gidx, (repo, lic) in sl:
        if repo in processed:
            continue
        if time.time() - t0 > TIME_BUDGET:
            log("time budget reached"); break
        if repos_done >= BATCH_REPOS:
            log("batch cap reached"); break
        if B.free_gb() < C.MIN_FREE_DISK_GB:
            shutil.rmtree(C.NUGET, ignore_errors=True)
            os.makedirs(C.NUGET, exist_ok=True)
            if B.free_gb() < C.MIN_FREE_DISK_GB:
                log("low disk -> stop"); break
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
        log(f"[s{SHARD} g{gidx}] {repo:42s} kept={stat.get('kept',0):3d} "
            f"reason={stat.get('reason')} buf={len(buf)} t={time.time()-t0:.0f}s")
        if len(buf) >= 1500 or repos_done % 15 == 0:
            if buf:
                if flush_shard(buf, man, sidx):
                    buf = []
                    sidx += 1
            else:
                save_manifest(man)
    if buf:
        flush_shard(buf, man, sidx)
    save_manifest(man)
    log(f"=== shard {SHARD} done | +{repos_done} repos | tasks={man['counts'].get('tasks')} "
        f"built={man['counts'].get('repos_built')} ===")


if __name__ == "__main__":
    main()
