# Credits

The AI day trader (`daytrader/`) was designed after surveying open-source LLM trading agents and
self-improving agent research. No source files were copied. The code and prompts here are new and
written for this project, but several designs, and a few short phrases in the prompts, are adapted
from the projects below. Each project keeps its own license and copyright; links point to the
originals.

| Project | License | What was adapted (and changed) |
|---|---|---|
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | Apache-2.0 | The analyst → trader → post-trade reflection flow. Its bull/bear debate became one bull case and one bear case inside each research call. From its trader prompt: state entries and stops as absolute price levels and reason only from the evidence given. Its point-in-time rule that memories are used only after their outcome is known. All rewritten and condensed for one model. |
| [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | MIT | "The LLM proposes, code decides": shares, caps and loss limits are computed in code, never by the model. Caching every model call by prompt hash. Its persona "hard rule" to treat the shown date as the present and not use later knowledge (reworded). |
| [pipiku915/FinMem-LLM-StockTrading](https://github.com/pipiku915/FinMem-LLM-StockTrading) | MIT | Layered memory scored by recency + importance + relevance, with promotion between layers. Here: two layers, BM25 instead of embeddings, trading-day half-lives. |
| [ACE: Agentic Context Engineering](https://arxiv.org/abs/2510.04618) ([code](https://github.com/ace-agent/ace)) | paper / see repo | An itemized playbook whose rules carry helpful/harmful counters, updated through small delta operations instead of full rewrites (to avoid "context collapse"). |
| [mnemox-ai/tradememory-protocol](https://github.com/mnemox-ai/tradememory-protocol) | MIT | Showing the proposer a "graveyard" of retired rules so it doesn't keep reinventing them, and outcome-weighted judgment of rules. |
| [zjzJoez/trading-agent](https://github.com/zjzJoez/trading-agent) | MIT | Driving a trading agent through `claude -p` on a Claude subscription, and keeping learning changes small and gated by statistics. |
| [DanielSaarva/orb-backtest-audit](https://github.com/DanielSaarva/orb-backtest-audit) | MIT | Its documented same-bar fill bug. Here the target is never credited on the entry bar, and a bar that touches both stop and target counts as a stop. |
| [HephaestLab/TraderHarness](https://github.com/HephaestLab/TraderHarness), [bettyguo/agent-backtest-lab](https://github.com/bettyguo/agent-backtest-lab) | Apache-2.0 | One data gateway that clamps every read to "as of" time (`daytrader.data.View`). |
| [AI4Finance-Foundation/FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | Apache-2.0 | Treating headlines as untrusted text and sanitizing them before they reach a prompt. |
| [Profit Mirage (arXiv 2510.07920)](https://arxiv.org/abs/2510.07920) | paper | The evidence that LLM trading results before a model's training cutoff are inflated by memorization. That is why backtests on those dates are refused by default. |

The Claude CLI runner (`daytrader/llm.py`) keeps the lean-call recipe from this repository's earlier
Kalshi bot (lean flags, exact model IDs, clean environment).
