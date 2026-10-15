# Chat Session Management REST Routes (`/api/chat/sessions`)

**Source Files**:
- `routes/chat/__init__.py`
- `routes/chat/chat_sessions.py`
- Helpers & Models: `utils/chat_sessions.py`, `models/chat.py`

These REST endpoints manage persistent multi-turn clipping threads stored in Firestore via Google ADK 2.0's `FirestoreSessionService` (`app_name = "clipping_studio"`).

---

## 1. Endpoints Overview

| Method | Path | Response Model | Status | Purpose |
| :---: | :--- | :--- | :---: | :--- |
| `GET` | `/api/chat/sessions` | `list[ChatSessionSummary]` | `200` | Lists lightweight session headers for the left sidebar (newest first). |
| `POST` | `/api/chat/sessions` | `ChatSessionSummary` | `201` | Explicitly creates a new chat session with optional pre-pinned `selected_video_ids`. |
| `GET` | `/api/chat/sessions/{session_id}` | `ChatSessionDetail` | `200` | Loads full message history, agent reasoning/tool traces, and re-signed `ClipCandidate` cards. |
| `PATCH` | `/api/chat/sessions/{session_id}` | `ChatSessionSummary` | `200` | Renames a session thread or updates its pinned `selected_video_ids` Tape Selector scope. |
| `DELETE` | `/api/chat/sessions/{session_id}` | *Empty* | `204` | Deletes the ADK session document and its `events` subcollection. |

---

## 2. Endpoint Details

### `GET /api/chat/sessions`
Fetches all chat sessions belonging to `user.uid` via `session_service.list_sessions` and converts each into a lightweight `ChatSessionSummary` (omitting the heavy `events` / `messages` array so sidebar rendering stays fast).

- **Auth**: `Bearer <Firebase-JWT>`
- **Ordering**: Reverses ADK's oldest-first list so the most recently updated session appears first.

#### Response Body (`list[ChatSessionSummary]`)
```json
[
  {
    "id": "sess_01hxyz",
    "uid": "user_123",
    "title": "Find the Waterfowl Dance dodge tutorial",
    "selected_video_ids": ["8f9d2a1b3c4e5f6a"],
    "created_at": "2026-10-15T14:20:00Z",
    "updated_at": "2026-10-15T14:22:15Z"
  }
]
```

---

### `POST /api/chat/sessions`
Creates a new ADK session in Firestore initialized with `title` (defaults to `"NEW CLIPPING LOG"`) and validated `selected_video_ids`.

> **Note**: In normal UI usage, opening the Chat view starts in a local draft state and lazily creates the Firestore session on the first `USER_MESSAGE` over WebSocket (`/api/chat/stream`). This REST endpoint supports explicit session creation when needed.

- **Auth**: `Bearer <Firebase-JWT>`
- **Request Body (`CreateChatSessionRequest`)**:
  ```json
  {
    "title": "Elden Ring Highlights",
    "selected_video_ids": ["8f9d2a1b3c4e5f6a"]
  }
  ```
- **Validation (`validate_owned_video_ids`)**: Verifies every ID in `selected_video_ids` exists in `lfvideos`, belongs to `user.uid`, and has `status == "SUCCESSFUL"` (`400` if still processing, `403` if unowned, `404` if missing).

---

### `GET /api/chat/sessions/{session_id}`
Loads the full ADK `Session` (including all persisted `session.events`) and reconstructs the React UI's `ChatSessionDetail` via `session_to_chat_detail(session)`:
1. **User & Model Message Bubbles (`ChatMessageModel`)**: Groups non-partial events by turn and flags turns interrupted via `[ STOP ]` (`interrupted: true`).
2. **Collapsible Agent Trace Drawer (`trace: list[AgentTraceStep]`)**:
   - Coalesces consecutive model reasoning `thought` parts into `THOUGHT` steps.
   - Extracts `function_call` parts into `TOOL_CALL` steps (`CALLING <tool_name>`).
   - Extracts `function_response` parts into `TOOL_RESULT` steps (`COMPLETED <tool_name>` or `ABORTED <tool_name>`).
3. **Inline Clip Candidate Cards (`clip_candidates: list[ClipCandidate]`)**:
   - Extracts proposed clips from `propose_clip_candidate` tool responses and batch-fetches all referenced `lfvideos` documents in **1 Firestore RPC** (`db.get_all`) to mint fresh v4 signed `preview_url`s.

---

### `PATCH /api/chat/sessions/{session_id}`
Updates `title` and/or `selected_video_ids` on an existing session.

- **Performance Optimization**: Calls `get_verified_adk_session(..., include_events=False)` which passes `GetSessionConfig(num_recent_events=0)` so Firestore skips reading the `events` subcollection.
- **State Persistence (`update_session_state_fields`)**: Appends a lightweight `author="system"` event carrying `EventActions(state_delta=updates)` to persist state changes in ADK's `FirestoreSessionService`.

#### Request Body (`UpdateChatSessionRequest`)
```json
{
  "title": "Renamed Studio Log",
  "selected_video_ids": ["8f9d2a1b3c4e5f6a"]
}
```

---

### `DELETE /api/chat/sessions/{session_id}`
Verifies session ownership (`include_events=False`) and calls `session_service.delete_session` to remove both the session document and its `events` subcollection in Firestore.

- **Response Code**: `204 No Content`
