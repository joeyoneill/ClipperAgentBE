# Automated Unit & Integration Testing (`tests/`)

This document covers the design, architecture, and execution of the automated `pytest` unit and integration test suite in `tests/`.

---

## 1. Overview & Coverage Target

The test suite validates all application layers without requiring live network calls to Google Cloud Platform (GCP) during test runs:

- **Framework**: `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `pytest-cov`, and FastAPI `TestClient` (`httpx`).
- **Coverage Requirement**: Enforced minimum of **`80.0%`** line coverage in `pyproject.toml` (`[tool.coverage.report] fail_under = 80`).
- **Current Coverage**: **`~89.6%`** across `agent/`, `models/`, `routes/`, `utils/`, and `main.py`.

---

## 2. Test Isolation & Environment Strategy (`tests/conftest.py`)

### Strict `.env` Enforcement (No Hardcoded Project Defaults)
`tests/conftest.py` loads configuration directly from `.env` via `python-dotenv` (`load_dotenv()`) and verifies that all required environment variables (`GCP_PROJECT_ID`, `FIRESTORE_DB_NAME`, `GCS_ROOT_BUCKET_NAME`, `MODEL_ARMOR_TEMPLATE_NAME`, collection names, and model names) are defined. No project IDs, bucket names, or service account emails are hardcoded in the test files.

### Offline GCP SDK Mocking
Several backend modules initialize GCP SDK clients at import time (`db.py`, `storage.py`, `utils/lf_videos.py`, `utils/chat_sessions.py`, `agent/tools/helpers.py`, `utils/observability.py`). Before importing `main.app`, `tests/conftest.py` patches:

| SDK Constructor | Application Singleton Isolated |
| :--- | :--- |
| `google.cloud.firestore.Client` | `db.db` (`mock_db`) |
| `google.cloud.firestore.AsyncClient` | `utils.chat_sessions.session_service` |
| `google.cloud.storage.Client` | `storage.gcs_client` (`mock_gcs`) |
| `google.cloud.run_v2.JobsClient` | `utils.lf_videos.run_jobs_client` |
| `google.genai.Client` | `agent.tools.helpers.genai_client` |
| `google.cloud.secretmanager.SecretManagerServiceClient` | `utils.secret_manager.get_secret` |
| `google.cloud.trace_v2.TraceServiceClient` | `utils.observability.setup_observability` |

### Shared Fixtures
- **`mock_user` / `other_user`**: Authenticated `UserInfo` payloads for testing user-scoped data access and `403 Forbidden` tenant isolation.
- **`client`**: FastAPI `TestClient` with `get_current_user` overridden to return `mock_user`.
- **`unauth_client`**: Unauthenticated `TestClient` to verify `401`/`403` rejection on protected routes.
- **`reset_global_mocks` (`autouse=True`)**: Resets `mock_db`, `mock_gcs`, and `app.dependency_overrides` before and after every test function.

---

## 3. Test Modules Breakdown

| Test File | Target Modules Tested | Key Scenarios Covered |
| :--- | :--- | :--- |
| **`tests/test_models.py`** | `models/misc.py`, `models/lf_videos.py`, `models/sf_videos.py`, `models/chat.py`, `agent/agent.py` | Pydantic schema validation, `WordTimestamp` float coercion, `LFVideo` & `SFVideo` state machine literals, `AspectRatioType` (`original`, `9:16_crop`, `9:16_pad`) & `ResolutionType` (`1080p`, `720p`) constraints, WebSocket frame schemas (`WSClientMessage`, `WSServerMessage`), and `ClipEditorInput`. |
| **`tests/test_subtitle_math_and_sf_utils.py`** | `utils/sf_videos.py` | **Subtitle Word-Timing Redistribution Math (`realign_transcript_words`)**: character-length weighted distribution when no prior words exist, `difflib.SequenceMatcher` opcodes (`equal`, `replace`, `insert` at start/middle/end, `delete`), relative vs. absolute timestamp normalization, full foreign-language translation clamping within `[clip_start_sec, clip_end_sec]`, and monotonic non-overlapping enforcement.<br>**Manual Timing Validation (`validate_manual_words`)**: `15ms` float rounding tolerance, out-of-window rejection, overlap detection, and zero-duration checks.<br>**ASS Karaoke & FFmpeg (`generate_karaoke_ass_file`, `render_sf_video_variant`)**: Vertical (`4` words/chunk) vs. landscape (`6` words/chunk) `.ass` subtitle generation, active-word yellow (`{\c&H0000F0FF&}`) tags, async FFmpeg filter graphs, and temporary file cleanup. |
| **`tests/test_utils.py`** | `utils/auth.py`, `utils/lf_videos.py`, `utils/chat_sessions.py`, `utils/secret_manager.py`, `utils/observability.py` | Firebase JWT verification (HTTP `Bearer` + WebSocket `1008` policy errors), GCS v4 signed URL generation (local key `sign_bytes` vs. Cloud Run IAM token refresh), ADK `Session` to `ChatSessionDetail` event reconstruction (coalesced thoughts, tool call/result traces, `ClipCandidate` extraction, `INVOCATION_ABORTED` handling), Secret Manager fallback, and Cloud Trace JSON structured logging. |
| **`tests/test_routes_lf_videos.py`** | `main.py`, `routes/lf_videos/upload.py`, `routes/lf_videos/read.py`, `routes/lf_videos/delete.py` | `GET /health`, `POST /api/lf_videos/upload/init` (`201`, `415` bad MIME, `502` GCS failure with Firestore rollback), `POST /api/lf_videos/upload/{vid}/complete` (Cloud Run job trigger + `400`/`500` error paths), `POST /api/lf_videos/upload/{vid}/failed` (`200` and `409` conflict), `GET /api/lf_videos/list`, and `DELETE /api/lf_videos/{vid}` cascading delete (GCS blobs + batched Firestore segments + parent doc). |
| **`tests/test_routes_sf_videos.py`** | `routes/sf_videos/save.py`, `routes/sf_videos/manage.py` | `POST /api/sf_videos/save` (`PENDING_CUT` creation + `(uid, candidate_id)` idempotency check), `GET /api/sf_videos/list` (batch-signed preview URLs), `PATCH /api/sf_videos/{sf_id}` (Mode A simple transcript realignment, Mode B manual word timing, and automatic invalidation/deletion of cached `*__subs_on` MP4 variants), `DELETE /api/sf_videos/{sf_id}`, and `POST /api/sf_videos/{sf_id}/download` (`cached=True` fast path vs. `cached=False` FFmpeg render path). |
| **`tests/test_routes_chat.py`** | `routes/chat/chat_sessions.py`, `routes/chat/stream.py` | CRUD endpoints for `/api/chat/sessions` (`GET`, `POST`, `PATCH`, `DELETE`) and bidirectional WebSocket streaming on `/api/chat/stream`: first-frame `AUTH` handshake, lazy session creation on first `USER_MESSAGE`, streamed `AGENT_THOUGHT`, `TRACE_STEP`, `CLIP_CANDIDATE`, `AGENT_TOKEN`, `TURN_COMPLETE`, and live controls (`SET_VIDEOS`, `INTERRUPT`, `ADD_CONTEXT`). |
| **`tests/test_agent_tools.py`** | `agent/tools/helpers.py`, `agent/tools/list_available_videos.py`, `agent/tools/search_video_segments.py`, `agent/tools/get_expanded_window.py`, `agent/tools/propose_clip_candidate.py`, `agent/agent.py` | `ToolContext` state enforcement (`ALL_VAULT_TAPES` vs. `PINNED_TAPES`), `search_video_segments` Reciprocal Rank Fusion (`rrf_score`) across `text` and `visual` embeddings, `get_expanded_segment_window` $\pm 2$ chunk stitching, `propose_clip_candidate` multi-segment word slicing within `[start_sec - 0.05, end_sec + 0.05]`, and `ModelArmorPlugin` registration. |

---

## 4. Running the Tests

```bash
# Run all tests (tests/ + evals/) with terminal coverage report
uv run pytest -v --cov

# Run only unit & integration tests in tests/
uv run pytest tests/ -v --cov

# Run a single test file or function
uv run pytest tests/test_subtitle_math_and_sf_utils.py -k "test_realign_transcript_words"
```

### Generated Local Artifacts (`.coverage` and `.pytest_cache/`)
- **`.coverage`**: A local SQLite file created by `pytest-cov` (`coverage.py`) to record line execution counts during test runs.
- **`.pytest_cache/`**: Local pytest state directory caching last-run test statuses.
- Both are listed in `.gitignore` and should never be committed to version control.
