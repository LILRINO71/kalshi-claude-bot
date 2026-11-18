# AI agent tools: CLI-Anything and REA for Kalshi Claude Desk

Research note, 2026-11-17. It covers two agent toolkits:

- **[CLI-Anything](https://github.com/HKUDS/CLI-Anything)** (Apache-2.0) wraps real desktop programs (FreeCAD, Blender, 3MF and others) in CLIs that agents can drive.
- **[REA](https://github.com/morluto/rea)** (MIT) is an MCP server and CLI that gives agents reverse-engineering tools.

This file covers only what matters for **this** repo.

How to read the labels:

- **Confirmed**: checked against the tool's own repo or docs.
- **Recommendation**: a suggestion, not tested.
- Nothing below was run end to end on this project yet.

## What could help here

| Tool | Use in this repo | Value |
|---|---|---|
| REA **`environment-build`** | Record a HAR of the desk running in **paper mode**, then turn it into an agent environment, then build a repeatable offline eval for the Claude trader (fixed market replay plus reward checks). This is a strong AI/ML portfolio piece. **Confirmed**: REA turns recorded requests and responses into callable tools and exports an AgentEnv bundle | Medium-High |
| REA **SQLite inspector** | Inspect any SQLite stores under `data/`. **Confirmed**: needs Node 24.15+ (or 26+) | Low |
| CLI-Anything **ollama** harness | A local-model baseline to compare against Claude in backtests | Low |

## Rules for this repo

- Use Kalshi's **documented** API only. Don't reverse private endpoints.
- Strip credentials from any HAR before REA reads it. REA's results go to the model provider.
- This project stays paper-only.

## Proposed changes (not made yet)

1. Add `docs/agent-env.md` describing: record a paper session HAR (with secrets removed), run `rea environment-build`, then add tasks and reward checks.

## Status on the dev PC (updated 2026-11-17)

- **Installed and working:**
  - Agent skills `cli-hub-meta-skill` and `reverse-engineer-anything`.
  - `cli-anything-hub` 0.4.1.
  - Node 24.22.1.
  - REA 6.9.0 (global `rea`), registered as an MCP server in Claude Code. `rea doctor` reports it healthy.
  - FreeCAD 1.1.4, with the `cli-anything-freecad` harness (`FREECAD_PATH` points at `FreeCADCmd.exe`).
- **Smoke-tested:**
  - REA's JS analysis of the shot-sim build.
  - A FreeCAD cube built and measured (volume and center of mass correct), then exported to STEP through headless FreeCAD.
- **Not installed:** REA's optional engines (Ghidra, IDA, JADX/JDK 17+, Kuna), adb, and Blender.

## Rules for REA

Use REA only on code and builds we made, or on devices we own. Reverse engineering commercial software can break its EULA or DMCA §1201, and §1201(f) covers only narrow interoperability work. REA's tool results are sent to the AI model provider, so never point it at keys, `.env` files or credentials.
