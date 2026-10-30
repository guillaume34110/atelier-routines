# Project instructions

- Never open, parse or follow `CLAUDE.md`.
- Prefix shell commands with `rtk`; use `rtk proxy` for unfiltered commands.
- Share the catalogue and executor between CLI and MCP. Keep two compact MCP tools.
- Prefer installed dependencies and the Python standard library. No extra model.
- A routine accepts a fixed schema and allowed operations only: no shell snippets, eval, arbitrary connector calls or permission expansion.
- Preserve sources, data, secrets, Git changes and active services. Enforce installed storage/workspace guards.
- Keep output bounded, preserve detailed evidence, and report truncation and every non-success state explicitly.
- For every covered operational step, including one-off tasks and subagents, use MCP before an equivalent native command. Known IDs need no lookup: git.status/diff/staged/log/branches, project.manifest/tree/search, files.logs/json/csv. Otherwise search once. CLI is the fallback when MCP is unavailable. Exact source/patch reads for editing and operations outside coverage remain direct. A question alone never starts an operation.
- Preserve execution references; resume only known operations. Never present a stale or incomplete receipt as current validation.
- Read `.github/workflows/checks.yml` before pushing. Run its complete regression and security checks on the final revision.
- Use configured Atelier VPS for portable checks under its resource policy. Keep Git, personal credentials and macOS-specific integration checks local. CI workers enforce their own job budget.
- Push work branches through a PR; require green `verify` and `security` checks on the latest revision, an up-to-date branch and administrator enforcement. Never bypass protections.
- Use the configured human Git identity, with no generated authorship attribution. Do not create `codex/` branches.
