# ===== C# repair-task builder -- ONE Kaggle notebook = ONE shard =====
# Paste this whole cell into a Kaggle CPU notebook (Settings > Internet = ON,
# and add an Add-ons > Secret named "HF_TOKEN" with your HF write token).
#
# CHANGE ONLY ONE NUMBER: SHARD. Each notebook gets a unique shard in 20..63.
# (GitHub Actions already covers shards 0..19 of the same 64-way split, so
#  Kaggle shards 20+ never overlap with Actions -> no duplicate work.)

SHARD   = 20        # <-- unique per notebook (20, 21, 22, ... up to 63)
NSHARDS = 64        # shared with Actions; do not change
HOURS   = 11        # run ~11h (under Kaggle's 12h session cap)

import os, subprocess, sys

# 1) install the .NET SDK (10.0) into the notebook's writable dir
DOTNET = "/kaggle/working/.dotnet"
if not os.path.exists(DOTNET + "/dotnet"):
    os.system("curl -sSL https://dot.net/v1/dotnet-install.sh "
              f"-o /tmp/di.sh && bash /tmp/di.sh --channel 10.0 --install-dir {DOTNET}")
os.environ["PATH"] = DOTNET + ":" + os.environ.get("PATH", "")
os.environ["DOTNET_ROOT"] = DOTNET

# 2) pull the pipeline (public repo)
B = "/kaggle/working/builder"
if not os.path.exists(B):
    os.system(f"git clone -q --depth 1 https://github.com/nurric-ai/csharp-dotnet-repair-builder {B}")
os.chdir(B)
os.system("git -C /kaggle/working/builder pull -q 2>/dev/null || true")

# 3) python deps + build the Roslyn mutator
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyarrow", "huggingface_hub"], check=True)
subprocess.run(["dotnet", "publish", "mutator/mutator.csproj", "-c", "Release", "-o", "mutator_pub"], check=True)

# 4) HF token from the Kaggle secret
try:
    from kaggle_secrets import UserSecretsClient
    os.environ["HF_TOKEN"] = UserSecretsClient().get_secret("HF_TOKEN")
    print("HF_TOKEN loaded from Kaggle secret")
except Exception as e:
    print("WARNING: could not load HF_TOKEN secret --", e)

# 5) run this shard (disjoint slice = candidate_index % 64 == SHARD)
os.environ.update({
    "SHARD": str(SHARD), "NSHARDS": str(NSHARDS),
    "TIME_BUDGET_S": str(int(HOURS * 3600)), "BATCH_REPOS": "99999",
    "CPT_MUTATOR_DLL": f"{B}/mutator_pub/mutator.dll",
    "CPT_MUTATOR_DIR": f"{B}/mutator_pub",
    "CPT_CANDIDATES": f"{B}/candidates.txt",
    "CPT_REPAIR_ROOT": "/kaggle/working/data",
    "NUGET_PACKAGES": "/kaggle/working/nuget",
    "DOTNET_ROLL_FORWARD": "Major", "DOTNET_NOLOGO": "true",
    "MSBUILDDISABLENODEREUSE": "1",
})
sys.path.insert(0, "src")
import gh_runner
gh_runner.main()
print(f"SHARD {SHARD} finished.")
