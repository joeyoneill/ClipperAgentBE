# CLIP_AGENT_OS Backend Documentation (`docs/`)

Welcome to the technical documentation hub for **`capstone_be`** — the FastAPI + Google ADK 2.0 backend powering the **CLIP_AGENT_OS** Short-Form Video Clipping & Multimodal RAG Studio.

---

## Documentation Directory

### 1. API Routes & Endpoints (`docs/routes/`)
| Document | Status | Description |
| :--- | :---: | :--- |
| **[`routes/README.md`](./routes/README.md)** | Completed | API architecture overview, Firebase JWT auth (`HTTP Bearer` & WebSocket `AUTH` handshake), `/health` check, and complete endpoint routing map. |
| **[`routes/lf_videos.md`](./routes/lf_videos.md)** | Completed | **Long-Form Video Routes (`/api/lf_videos`)**: Resumable GCS upload init, completion & Cloud Run Job trigger, failure handling, Vault listing, and cascading deletion. |
| **[`routes/sf_videos.md`](./routes/sf_videos.md)** | Completed | **Short-Form Video / Reel Routes (`/api/sf_videos`)**: Human-in-the-loop clip saving, dual-mode subtitle editing (`realign_transcript_words` & `validate_manual_words`), cache invalidation, and on-demand FFmpeg export. |
| **[`routes/chat_sessions.md`](./routes/chat_sessions.md)** | Completed | **Chat Sessions REST API (`/api/chat/sessions`)**: Sidebar session summaries, ADK event history reconstruction, Tape Selector scope updates, and session deletion. |
| **[`routes/chat_stream.md`](./routes/chat_stream.md)** | Completed | **Chat WebSocket Streaming (`WS /api/chat/stream`)**: Bidirectional event protocol, lazy draft session creation, live trace/clip streaming, and mid-turn `INTERRUPT` / `ADD_CONTEXT` controls. |

### 2. Quality Assurance & AI Evaluation
| Document | Status | Description |
| :--- | :---: | :--- |
| **[`testing.md`](./testing.md)** | Completed | Unit & integration test suite (`tests/`), offline GCP client isolation, subtitle word-timing math verification, WebSocket/REST route testing, and `>80%` coverage guide. |
| **[`evaluations.md`](./evaluations.md)** | Completed | Automated AI Evaluation Suite (`evals/`), `golden_dataset.json` schema, North Star metrics (`Recall@5`, `Precision@5`, Boundary Hygiene, 2-Layer Guardrail Block Rate, and Rubric Judge). |

### 3. Architecture & Operations (Planned)
| Document | Status | Description |
| :--- | :---: | :--- |
| *`architecture.md`* | *Planned* | System architecture, ADK 2.0 multi-agent topology (`orchestrator_agent` + single-turn `clip_editor_agent`), and Architecture Decision Records (ADRs). |
| *`deployment_and_infra.md`* | *Planned* | GCP infrastructure (`clipper-vpc`, Cloud Run Services & Jobs, Firestore KNN vector indexes, Model Armor, Secret Manager, and Cloud Trace observability). |

---

## Quick Verification Commands

Ensure your `.env` file is present in the project root (`capstone_be/.env`), then run:

```bash
# 1. Run full unit, integration, and AI evaluation test suite with coverage gate (>80%)
uv run pytest -v --cov

# 2. Run the standalone AI Evaluation North Star Scorecard CLI
uv run python -m evals.run_evals
```
