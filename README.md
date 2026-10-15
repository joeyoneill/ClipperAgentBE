# CLIP_AGENT_OS — FastAPI & Google ADK 2.0 Backend (`capstone_be`)

`capstone_be` is the core API, conversational multi-agent orchestration, and on-demand video rendering backend for **CLIP_AGENT_OS** — an agentic **Long-Form-to-Short-Form Video Clipping & Multimodal RAG Studio**.

Built on **Python 3.14+**, **FastAPI**, **Google ADK 2.0 (`google-adk`)**, **Vertex AI / Gemini**, **Firestore Vector Search (`1408d`)**, **Google Cloud Storage**, **Google Cloud Model Armor**, and **FFmpeg**, it powers real-time multimodal video Q&A, precision sentence-boundary clip extraction, dual-mode karaoke subtitle refinement, and multi-format MP4 rendering.

---

## 1. Key Capabilities & Responsibilities

- **Direct-to-Cloud Video Ingestion & Lifecycle Management (`/api/lf_videos`)**:
  - Generates short-lived **GCS v4 Signed `PUT` URLs** so raw long-form video binaries upload directly from the browser to Cloud Storage without passing through the API server.
  - Tracks lifecycle state (`UPLOADING` $\rightarrow$ `PROCESSING` $\rightarrow$ `SUCCESSFUL` / `FAILED`) in Firestore and dispatches the asynchronous **Video Processor Cloud Run Job** (`google.cloud.run_v2.JobsClient`) upon upload completion.
  - Provides Vault library listing with signed playback URLs and cascading deletion across GCS blobs, `lf_video_segments` vector chunks, and parent `lfvideos` records.
- **Hierarchical Google ADK 2.0 Multi-Agent Studio (`agent/` & `WS /api/chat/stream`)**:
  - **`orchestrator_agent` (Root Multi-Turn Agent, `temp=0.3`)**: Handles grounded video Q&A with `[MM:SS - MM:SS]` citations, manages session tape scoping (`ALL_VAULT_TAPES` vs. `PINNED_TAPES`), and delegates clipping requests.
  - **`clip_editor_agent` (Isolated Single-Turn Sub-Agent, `mode="single_turn"`, `temp=0.1`)**: Loads a 5-chunk (`150`-second) stitched word-level timeline (`[start_sec-end_sec] word`) via `get_expanded_segment_window`, selects natural sentence boundaries with zero mid-word cuts, and emits structured `ClipCandidate` preview cards via `propose_clip_candidate`—discarding the heavy word timeline after its single turn to keep multi-turn session history compact.
  - **Hybrid Multimodal Vector Search (`search_video_segments`)**: Queries `1408`-dimensional `gemini-embedding-2` vectors in Firestore using `find_nearest` (`COSINE`) across spoken dialogue (`text_embedding`), visual scenes (`video_embedding`), or both simultaneously fused via **Reciprocal Rank Fusion (RRF)**.
  - **Bidirectional WebSocket Streaming (`WS /api/chat/stream`)**: Streams live `AGENT_THOUGHT`, `TRACE_STEP`, `CLIP_CANDIDATE`, and `AGENT_TOKEN` frames with mid-turn **`INTERRUPT`** (`[ STOP ]`) and **`ADD_CONTEXT`** (`[ + STEER ]`) execution controls.
- **2-Layer Defense-in-Depth Guardrails**:
  - **Layer 1 (System Prompt Policy)**: Refuses out-of-domain queries (coding, math, trivia, recipes) in the studio producer persona with `0` tool calls.
  - **Layer 2 (`ModelArmorPlugin`)**: Screens inbound prompts, tool outputs, and model responses via **Google Cloud Model Armor** (`before_model_callback`, `after_model_callback`, `after_tool_callback`) against prompt injection, jailbreaks, malicious URIs, and Responsible AI violations.
- **Short-Form Reel Management, Dual-Mode Subtitle Editor & Cached FFmpeg Export (`/api/sf_videos`)**:
  - Persists saved clips in a lightweight `PENDING_CUT` state with `(uid, candidate_id)` idempotency.
  - Supports **Mode A (Simple Paragraph Text)** proportional timestamp redistribution (`realign_transcript_words` via `difflib.SequenceMatcher`) for typo fixes and foreign-language translations, and **Mode B (Manual Word Timing Table)** validation (`validate_manual_words`), automatically invalidating stale `*__subs_on` cached renders.
  - Renders and caches MP4 variants on demand across aspect ratios (`original` 16:9, `9:16_crop`, `9:16_pad` Gaussian blur-pad), resolutions (`1080p`, `720p`), and burned-in `.ass` active-word yellow (`{\c&H0000F0FF&}`) karaoke subtitles.
- **Observability & Automated Quality Assurance**:
  - Exports OpenTelemetry distributed traces to **Google Cloud Trace** (`CloudTraceSpanExporter`) and emits trace-correlated single-line JSON logs (`CloudLoggingJsonFormatter`).
  - Includes a comprehensive `pytest` unit/integration suite (`tests/`, `~89.6%` coverage) and a 4-pillar **Automated AI Evaluation Suite** (`evals/`).

---

## 2. Directory Structure

```text
capstone_be/
├── main.py                      # FastAPI app entrypoint, CORS middleware, /health probe & observability init
├── db.py                        # Firestore Native client initialization
├── storage.py                   # Google Cloud Storage client initialization
├── Dockerfile                   # Python 3.14 + uv + FFmpeg + DejaVu fonts container definition
├── pyproject.toml               # Dependencies, pytest configuration, and >=80% coverage gate
├── agent/                       # Google ADK 2.0 multi-agent system
│   ├── agent.py                 # orchestrator_agent, clip_editor_agent (single_turn) & ModelArmorPlugin
│   ├── prompts/                 # Static system prompts (optimized for Gemini prefix caching)
│   └── tools/                   # ADK tools: list_available_videos, search_video_segments,
│                                # get_expanded_window, propose_clip_candidate & zero-trust helpers
├── models/                      # Pydantic v2 request/response schemas & state machine literals
│   ├── lf_videos.py             # Long-form upload, Vault listing & lifecycle models
│   ├── sf_videos.py             # Reel clip save, subtitle patch & FFmpeg export models
│   ├── chat.py                  # REST session history & bidirectional WebSocket frame models
│   └── misc.py                  # UserInfo JWT identity & shared primitives
├── routes/                      # FastAPI HTTP REST & WebSocket routers
│   ├── lf_videos/               # POST /upload/init, /complete, /failed, GET /list, DELETE /{vid}
│   ├── sf_videos/               # POST /save, GET /list, PATCH /{sf_id}, DELETE /{sf_id}, POST /download
│   └── chat/                    # CRUD /api/chat/sessions & WebSocket /api/chat/stream
├── utils/                       # Core domain & infrastructure utilities
│   ├── auth.py                  # Firebase JWT verification (HTTP Bearer & WebSocket AUTH handshake)
│   ├── lf_videos.py             # Keyless v4 Signed URLs, Cloud Run Job trigger & cascading delete
│   ├── sf_videos.py             # Subtitle realignment math, .ass karaoke generator & FFmpeg renderer
│   ├── chat_sessions.py         # FirestoreSessionService wrapper & ADK event history reconstruction
│   ├── secret_manager.py        # Runtime GCP Secret Manager resolver with .env fallback
│   └── observability.py         # OpenTelemetry Cloud Trace exporter & trace-correlated JSON logger
├── tests/                       # Offline-isolated unit & integration test suite (pytest)
├── evals/                       # 4-pillar deterministic + rubric AI Evaluation Suite & golden dataset
└── docs/                        # Comprehensive technical documentation hub
```

> **Detailed Technical Documentation**: See **[`docs/README.md`](./docs/README.md)** for in-depth documentation on [System Architecture & ADRs](./docs/architecture.md), [Cloud Deployment & Infrastructure](./docs/deployment_and_infra.md), [API Routes](./docs/routes/README.md), [Unit & Integration Testing](./docs/testing.md), and [AI Evaluations](./docs/evaluations.md).

---

## 3. Prerequisites & Environment Configuration

### Prerequisites
- **Python `3.14+`** and **[`uv`](https://docs.astral.sh/uv/)** package manager.
- **FFmpeg** (bundled automatically via `imageio-ffmpeg` for local runs, and installed via `apt` with `libass` / `fonts-dejavu-core` inside `Dockerfile`).
- **Google Cloud SDK (`gcloud`)** authenticated against your target GCP sandbox project.

### Environment Variables (`.env`)
Create a `.env` file in `capstone_be/` with the following configuration variables (replace angle-bracket placeholders with your environment's values):

```ini
# Google Cloud Project & Region
GCP_PROJECT_ID="<YOUR_GCP_PROJECT_ID>"
GCP_REGION="<YOUR_GCP_REGION>"

# Firestore Native Database & Collection Names
FIRESTORE_DB_NAME="<YOUR_FIRESTORE_DB_NAME>"
LFVIDEO_COLLECTION_NAME="lfvideos"
LFVIDEO_SEGMENT_COLLECTION_NAME="lf_video_segments"
CHAT_SESSION_COLLECTION_NAME="chat_sessions"
SFVIDEO_COLLECTION_NAME="sfvideos"

# Google Cloud Storage & Cloud Run Job
GCS_ROOT_BUCKET_NAME="<YOUR_GCS_BUCKET_NAME>"
VIDEO_PROCESSOR_JOB_NAME="<YOUR_VIDEO_PROCESSOR_JOB_NAME>"
SIGNING_SERVICE_ACCOUNT_EMAIL="<YOUR_BACKEND_SERVICE_ACCOUNT_EMAIL>"

# Google Cloud Model Armor Guardrail Template
MODEL_ARMOR_TEMPLATE_NAME="projects/<YOUR_GCP_PROJECT_ID>/locations/<YOUR_GCP_REGION>/templates/<YOUR_TEMPLATE_ID>"

# Gemini & Multimodal Embedding Models
ORCHESTRATOR_MODEL_NAME="gemini-3.5-flash"
CLIPPER_SUBAGENT_MODEL_NAME="gemini-3.5-flash"
EMBEDDING_MODEL_NAME="gemini-embedding-2"
GEMINI_API_KEY="<YOUR_GEMINI_API_KEY>"

# Allowed CORS Origins (comma-separated)
FRONTEND_URL="http://localhost:5173,http://localhost:8080"
```

---

## 4. Running Locally

### Step 1: Authenticate via Keyless Service Account Impersonation (ADC)
No static service account JSON keys are required on disk. Authenticate using Application Default Credentials by impersonating the backend service account:

```bash
gcloud auth application-default login \
  --impersonate-service-account="<YOUR_BACKEND_SERVICE_ACCOUNT_EMAIL>"
```

### Step 2: Install Dependencies & Start the FastAPI Server
```bash
cd capstone_be

# Install Python dependencies into the local .venv
uv sync

# Start the FastAPI server with hot-reload on port 8000
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```
- **Interactive OpenAPI Docs (Swagger UI)**: `http://localhost:8000/docs`
- **Health Check Endpoint**: `http://localhost:8000/health`

---

## 5. Running Tests & AI Evaluations

```bash
# 1. Run the full unit, integration, and AI evaluation suite with coverage gate (>= 80%)
uv run pytest -v --cov

# 2. Run only unit & integration tests
uv run pytest tests/ -v --cov

# 3. Run the standalone AI Evaluation North Star Scorecard CLI
uv run python -m evals.run_evals
```

---

## 6. Cloud Deployment

In production, `capstone_be` runs as a **VPC-internal Cloud Run Service** (`INGRESS_TRAFFIC_INTERNAL_ONLY`) with session affinity enabled, `2 vCPU / 2 GiB RAM`, and a `900s` request timeout.

### Option A: Build & Deploy Container Image Directly
```bash
# 1. Build and push the container image to Artifact Registry via Cloud Build
gcloud builds submit \
  --tag "<GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REPO>/<BACKEND_IMAGE_NAME>:<TAG>" \
  --project "<GCP_PROJECT_ID>"

# 2. Update the Cloud Run Service with the new image revision
gcloud run services update "<BACKEND_SERVICE_NAME>" \
  --image "<GCP_REGION>-docker.pkg.dev/<GCP_PROJECT_ID>/<ARTIFACT_REPO>/<BACKEND_IMAGE_NAME>:<TAG>" \
  --region "<GCP_REGION>" \
  --project "<GCP_PROJECT_ID>"
```

### Option B: Provision Full Stack via Terraform IaC
From the repository root `terraform/` directory:
```bash
cd ../terraform
cp terraform.tfvars.example terraform.tfvars
# Populate terraform.tfvars with your environment values, then run:
terraform init
terraform apply -var-file="terraform.tfvars"
```
See [`docs/deployment_and_infra.md`](./docs/deployment_and_infra.md) for the complete infrastructure and CI/CD runbook.
