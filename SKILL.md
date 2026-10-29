---
name: atelier-routines
description: Select and reuse bounded operational routines for recurring project, Git, QA, files, audio and connected-app tasks.
---

# Atelier routines

For recurring operational work, use the known ID directly with MCP `routines_run(id, params)`.
If the ID is unknown, call `routines_find(query, limit=3)` once, then run the selected ID.
Do not also run a CLI search. A simple question does not authorize executing a routine.

`routines_find` reports each routine as `executable`, `guided` or `unavailable`, and gives its
parameter schema. The implemented inspections and private recipe/execution managers report their real prerequisites. Long verification is limited to the fixed, authorised Atelier VPS adapter.
For `guided`, apply the returned procedure only within the user's request and existing
authorization. Never treat `running`, `guided`, `partial`, `unavailable`, `error`, `timeout`, `decision_required` or `abandoned` as a passed check.
The three starter proposals remain excluded from selection.

When the MCP is unavailable, keep the existing CLI as fallback. Known IDs use the same
executor, without another lookup:

```sh
rtk proxy atelier-routines run git.status --params '{"project":"/absolute/project"}'
rtk proxy atelier-routines find "chercher code" --limit 1
```

The executor accepts absolute project roots under its installation parent by default;
file/path parameters are relative, and protected paths and symlinks are refused. MCP roots
are configured in `ATELIER_ROUTINES_ROOTS` (JSON list). Results include private evidence files
under `PROJECT/output/routines-evidence`; read the receipt for expanded details. A capped
scan is `partial`; a shortened response has `response_truncated=true`. Neither is implicit
validation. Git diffs return statistics only; text references are not a call graph.

Legacy CLI commands remain available. Otherwise make one targeted lookup only when needed:

```sh
rtk proxy atelier-routines match "diagnostiquer un test échoué"
rtk proxy atelier-routines list
rtk proxy atelier-routines list audio
rtk proxy atelier-routines show audio.audition
rtk proxy atelier-routines context --project /absolute/project
rtk proxy atelier-routines tree --project /absolute/project
rtk proxy atelier-routines outline --file /absolute/project/module.py
```

The executable is a link to `scripts/routines.py` in this skill. If it is outside PATH, run that script with `rtk proxy python3`.

`match` uses local French/English keywords, without a model or network. It returns one procedure and at most two alternative IDs; it never executes the operation. Select the appropriate result and carry out its command or steps within the user's authorization. If it misses, use a precise domain/ID or proceed directly; do not repeatedly query or read all catalogue entries. Simple conversation needs no lookup.

`list` shows domain counts; `list DOMAINE` gives a short index. `show ID` returns one entry from [references/catalogue.json](references/catalogue.json). The catalogue has 109 procedures plus three explicitly excluded starter proposals. Procedures may reuse an installed script, guard, skill or connected tool; they are not 100 independent programs. Replace uppercase command placeholders with actual, safely quoted arguments; never evaluate a template as shell code. Missing prerequisites or unavailable tools remain blockers, not successful runs.

Use the installed guards rather than rebuilding their shell commands:

```sh
rtk proxy atelier-routines preflight --project /absolute/project
rtk proxy atelier-routines storage --project /absolute/project --gib 2
rtk proxy atelier-routines budget --project /absolute/project --gib 0.1 -- rtk proxy python3 script.py
rtk proxy atelier-routines snapshot --project /absolute/project
```

`storage` takes the expected peak in GiB; `budget` takes the temporary output limit, at most 5 GiB. The invoked job must put temporary outputs in `TASK_WORKSPACE`. The guards retain their own refusal and preservation behavior. A guarded run is not a passing test.

`context` reports manifest scripts, workflow paths and Git status without running checks. Read applicable workflows before validation or integration. Prefer connected Atelier VPS tools for compatible work; `show system.vps` explains the fallback.

For music, `show audio.feedback` and `show audio.freeze` separate the audible target from technical evidence. Preserve approved material. Musical acceptance requires Guillaume's listening feedback.

This catalogue does not change project instructions, service ownership, required CI, resource limits or authorization for publishing. Add a procedure after demonstrated reuse, with its canonical command, inputs and honest validation boundary; keep task history outside this entry point.

Agent recipes: register an immutable bounded definition with `routines.register`, then call its `custom.NAME` ID. Use `routines.list` and `routines.manage` for inspection/enable/disable. Only allowed inspections can be composed; never submit shell code.

Use an optional stable `request_id` to recover the same demand. `execution_id` and `executions.inspect` retrieve its state and targeted proof pages without rerunning. Historical receipts do not assert current facts. File reuse requires current content/version/proof verification; never reuse live service/resource/authorization facts or old tests as current validation.

`reports.compare` requires explicit compatible execution IDs; persistent errors remain unexplained. `reports.validate` checks explicit JSON assertions, not new tests. `checks.regression` owns prerequisites, submit/wait/collect/SHA/assertions/release for this project via the existing VPS client. Uncertain jobs remain preserved; `executions.resume` only recovers known, unreleased computations. A simple question never launches any of these operations.
