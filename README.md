# csharp-dotnet-repair-builder (Actions)

Workflow that builds verified C#/.NET compile/test/repair tasks for
`Nottybro/csharp-dotnet-repair-lora-v1`. Each run processes a bounded, resumable
batch: clone -> baseline build+test -> inject one minimal mutation -> keep only if
the broken revision fails deterministically and the gold repair compiles + passes
all tests. Raw shards + manifest are pushed to the HF dataset repo.

Trigger: `workflow_dispatch` (with budget_minutes / max_repos) or the 4-hour schedule.
All third-party build/test code runs only on this isolated GitHub runner.
