# CLIP_AGENT_OS Backend Documentation (`docs/`)

Welcome to the technical documentation hub for **`capstone_be`** — the FastAPI + Google ADK 2.0 backend powering the **CLIP_AGENT_OS** Short-Form Video Clipping & Multimodal RAG Studio.

---

## Documentation Directory

| Document | Status | Description |
| :--- | :---: | :--- |
| **[`testing.md`](./testing.md)** | Completed | Unit & integration test suite (`tests/`), offline GCP client isolation, subtitle word-timing math verification, WebSocket/REST route testing, and `>80%` coverage guide. |
| **[`evaluations.md`](./evaluations.md)** | Completed | Automated AI Evaluation Suite (`evals/`), `golden_dataset.json` schema, North Star metrics (`Recall@5`, `Precision@5`, Boundary Hygiene, 2-Layer Guardrail Block Rate, and Rubric Judge). |

---

## Quick Verification Commands

Ensure your `.env` file is present in the project root (`capstone_be/.env`), then run:

```bash
# 1. Run full unit, integration, and AI evaluation test suite with coverage gate (>80%)
uv run pytest -v --cov

# 2. Run the standalone AI Evaluation North Star Scorecard CLI
uv run python -m evals.run_evals
```
