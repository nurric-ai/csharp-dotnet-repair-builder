"""Read-only candidate repo pool sourced from the CPT state.db.
We never modify that DB; we only SELECT distinct (repo, license) for allowed licenses.
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C


def candidate_repos(limit=None, offset=0):
    conn = sqlite3.connect(C.SOURCE_DB)
    placeholders = ",".join("?" * len(C.ALLOWED_LICENSES))
    rows = conn.execute(
        "SELECT DISTINCT repo, lower(trim(license)) lic FROM docs "
        "WHERE lower(trim(license)) IN (" + placeholders + ")",
        tuple(C.ALLOWED_LICENSES)
    ).fetchall()
    conn.close()
    seen = {}
    for repo, lic in rows:
        seen.setdefault(repo, lic)
    items = [(r, seen[r]) for r in sorted(seen)]  # deterministic order
    if offset:
        items = items[offset:]
    if limit:
        items = items[:limit]
    return items


def repo_url(repo):
    return "https://github.com/" + repo


def load_seeds(path=None):
    p = path or os.path.join(C.ROOT, "seeds.txt")
    out = []
    for ln in open(p, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        repo = parts[0].strip()
        lic = parts[1].strip() if len(parts) > 1 else "mit"
        if repo:
            out.append((repo, lic))
    return out


def filtered_pool(min_files=20, require_test=True, limit=None, offset=0):
    """Repos from the CPT pool with >=min_files and (optionally) a test-ish path.
    No quality-of-popularity signal exists in state.db, so this is a coarse filter;
    the build/test harness is the real gate."""
    conn = sqlite3.connect(C.SOURCE_DB)
    ph = ",".join("?" * len(C.ALLOWED_LICENSES))
    A = tuple(C.ALLOWED_LICENSES)
    q = ("SELECT repo FROM docs WHERE lower(trim(license)) IN (" + ph + ") "
         "GROUP BY repo HAVING COUNT(*) >= ?")
    args = list(A) + [min_files]
    if require_test:
        q += (" AND SUM(CASE WHEN path LIKE '%Test%' OR path LIKE '%/tests/%' "
              "OR path LIKE '%/test/%' THEN 1 ELSE 0 END) > 0")
    repos = sorted(r[0] for r in conn.execute(q, args).fetchall())
    licmap = {}
    for r in repos:
        row = conn.execute("SELECT lower(trim(license)) FROM docs WHERE repo=? LIMIT 1", (r,)).fetchone()
        licmap[r] = row[0] if row else "mit"
    conn.close()
    items = [(r, licmap[r]) for r in repos]
    if offset:
        items = items[offset:]
    if limit:
        items = items[:limit]
    return items


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    for repo, lic in candidate_repos(limit=n):
        print(f"{repo}\t{lic}")
