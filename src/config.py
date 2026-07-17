"""Config for the C#/.NET repair-task (LoRA expert) dataset builder.

Workspace is ~/csharp-repair-lora and is COMPLETELY SEPARATE from the CPT corpus
(~/.csharp-cpt). We only READ the CPT state.db (read-only) to seed repo candidates.
The CPT HF dataset and its data_v1/ + splits/ are never modified.
"""
import os

ROOT = os.path.expanduser(os.environ.get("CPT_REPAIR_ROOT", "~/csharp-repair-lora"))
SRC      = os.path.join(ROOT, "src")
OUT      = os.path.join(ROOT, "out")
SCRATCH  = os.path.join(ROOT, "scratch")
WORK     = os.path.join(ROOT, "work")
NUGET    = os.path.join(ROOT, "nuget-cache")
LOGS     = os.path.join(ROOT, "logs")
REPORTS  = os.path.join(ROOT, "reports")
VERIFIER = os.path.join(ROOT, "verifier")
LICENSES = os.path.join(ROOT, "licenses")
MUTATOR  = os.environ.get("CPT_MUTATOR_DIR", os.path.join(ROOT, "mutator"))
MUTATOR_DLL = os.environ.get(
    "CPT_MUTATOR_DLL",
    os.path.join(MUTATOR, "bin", "Release", "net10.0", "mutator.dll"))
for _d in (SRC, OUT, SCRATCH, WORK, NUGET, LOGS, REPORTS, VERIFIER, LICENSES, MUTATOR):
    os.makedirs(_d, exist_ok=True)

HF_REPO = "Nottybro/csharp-dotnet-repair-lora-v1"
HF_URL  = "https://huggingface.co/datasets/" + HF_REPO
_tok_path = os.path.expanduser("~/.cache/huggingface/token")
TOKEN = (open(_tok_path).read().strip() if os.path.exists(_tok_path)
         else os.environ.get("HF_TOKEN", ""))

SOURCE_DB = os.path.expanduser("~/csharp-cpt/scratch/state.db")  # read-only candidate pool
TOKENIZER = "Qwen/Qwen2.5-Coder-1.5B"

ALLOWED_LICENSES = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause",
                    "isc", "unlicense", "cc0-1.0", "ms-pl", "0bsd"}

# --- target category mix ---
MIX = {
    "compile_type_null_api":     0.35,
    "unit_test_failure":         0.30,
    "async_concurrency":         0.15,
    "linq_collections_perf":     0.10,
    "framework_di_ef_config":    0.10,
}
# mutator family -> published category
CATEGORY_OF = {
    "compile":   "compile_type_null_api",
    "logic":     "unit_test_failure",
    "async":     "async_concurrency",
    "linq":      "linq_collections_perf",
    "framework": "framework_di_ef_config",
}

SPLIT_RATIOS = {"train": 0.85, "validation": 0.10, "test": 0.05}

# --- operational limits ---
SHARD_ROWS          = 2000
MIN_FREE_DISK_GB    = 4.0
BUILD_TIMEOUT       = 300      # dotnet build (s)
TEST_TIMEOUT        = 600      # dotnet test (s)
RESTORE_TIMEOUT     = 600      # dotnet restore (s)
CLONE_TIMEOUT       = 180
MAX_MUTATIONS_PER_REPO = 25
PER_REPO_BUDGET_S      = 180      # hard wall-clock cap per repo -> a pathological repo
                                  # (huge/slow test suite) can never eat a whole run; tuned
                                  # for throughput (favors moving on to faster repos)
MAX_FILE_CHARS      = 120_000  # skip files larger than this for mutation
MIN_FILE_CHARS      = 40

# offline nuget + quiet dotnet
os.environ.setdefault("NUGET_PACKAGES", NUGET)
os.environ.setdefault("DOTNET_NOLOGO", "true")
os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "true")
os.environ.setdefault("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "true")
