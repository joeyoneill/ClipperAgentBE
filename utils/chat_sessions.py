# Firestore-backed ADK SessionService & Chat History Manager

# Imports
from dotenv import load_dotenv
from fastapi import HTTPException, status
from google.adk.events.event import Event
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session
from google.adk.sessions.state import State
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from typing import Any, Optional
from uuid import uuid4
import os

# Custom Dependencies
from db import db
from models.chat import (
    ChatMessageModel,
    ChatSessionDetail,
    ChatSessionSummary,
)
from utils.lf_videos import generate_video_signed_read_url
from utils.misc import get_utc_now

# Load Env Vars
load_dotenv()
CHAT_SESSION_COLLECTION_NAME = os.environ["CHAT_SESSION_COLLECTION_NAME"]
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]

# Subcollection Names under `chat_sessions/{session_id}`
ADK_EVENTS_SUBCOLLECTION = "adk_events"
UI_MESSAGES_SUBCOLLECTION = "messages"

################################################################
# Firestore ADK Session Service
################################################################

class FirestoreSessionService(BaseSessionService):
    """
    Persists ADK Session metadata in `chat_sessions/{session_id}`,
    raw ADK Events in `chat_sessions/{session_id}/adk_events/{event_id}`,
    and UI ChatMessageModels (with full traces) in `chat_sessions/{session_id}/messages/{msg_id}`.
    Enforces strict user ownership (uid) on all operations.
    """

    def __init__(self, collection_name: str = CHAT_SESSION_COLLECTION_NAME):
        self.collection_name = collection_name


    @property
    def _collection(self):
        return db.collection(self.collection_name)


    @staticmethod
    def _clean_state_for_storage(state: dict[str, Any]) -> dict[str, Any]:
        """Strips ephemeral 'temp:' keys before persisting state to Firestore."""
        return {
            k: v
            for k, v in (state or {}).items()
            if not str(k).startswith(State.TEMP_PREFIX)
        }


    @staticmethod
    def _delete_subcollection(
        parent_ref: firestore.DocumentReference, subcollection_name: str
    ) -> None:
        """Deletes all documents in a subcollection in batches of 400."""
        sub_ref = parent_ref.collection(subcollection_name)
        batch = db.batch()
        op_count = 0

        for doc in sub_ref.stream():
            batch.delete(doc.reference)
            op_count += 1
            if op_count >= 400:
                batch.commit()
                batch = db.batch()
                op_count = 0

        if op_count > 0:
            batch.commit()

    ############################################################
    # Core ADK BaseSessionService Methods (Used by ADK Runner)
    ############################################################

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        sid = session_id or str(uuid4())
        now = get_utc_now()
        now_ts = now.timestamp()

        initial_state = self._clean_state_for_storage(state or {})
        initial_state["uid"] = user_id
        selected_vids = initial_state.get("selected_video_ids", [])
        title = initial_state.get("title") or "NEW CLIPPING LOG"

        doc_data = {
            "id": sid,
            "app_name": app_name,
            "uid": user_id,
            "title": title,
            "selected_video_ids": selected_vids,
            "state": initial_state,
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        self._collection.document(sid).set(doc_data)

        return Session(
            id=sid,
            app_name=app_name,
            user_id=user_id,
            state=dict(initial_state),
            events=[],
            last_update_time=now_ts,
        )


    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        doc_ref = self._collection.document(session_id)
        doc_snap = doc_ref.get()
        if not doc_snap.exists:
            return None

        data = doc_snap.to_dict() or {}
        if data.get("uid") != user_id:
            # Strict tenant isolation: never return another user's session
            return None

        # Stream full event history from the `adk_events` subcollection ordered by timestamp
        events_query = doc_ref.collection(ADK_EVENTS_SUBCOLLECTION).order_by(
            "timestamp", direction=firestore.Query.ASCENDING
        )
        events: list[Event] = []
        for ev_doc in events_query.stream():
            raw_ev = ev_doc.to_dict() or {}
            try:
                events.append(Event.model_validate(raw_ev))
            except Exception:
                continue

        if config:
            if config.after_timestamp is not None:
                events = [
                    e for e in events if (e.timestamp or 0.0) >= config.after_timestamp
                ]
            if config.num_recent_events is not None:
                events = (
                    events[-config.num_recent_events :]
                    if config.num_recent_events > 0
                    else []
                )

        state = data.get("state") or {}
        state["uid"] = user_id
        state["selected_video_ids"] = data.get("selected_video_ids", [])

        updated_at = data.get("updated_at")
        last_update_ts = (
            updated_at.timestamp() if updated_at is not None and hasattr(updated_at, "timestamp") else 0.0
        )

        return Session(
            id=session_id,
            app_name=data.get("app_name", app_name),
            user_id=user_id,
            state=state,
            events=events,
            last_update_time=last_update_ts,
        )


    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: Optional[str] = None,
    ) -> ListSessionsResponse:
        query = self._collection.where(filter=FieldFilter("app_name", "==", app_name))
        if user_id:
            query = query.where(filter=FieldFilter("uid", "==", user_id))

        sessions: list[Session] = []
        for doc in query.stream():
            d = doc.to_dict() or {}
            updated_at = d.get("updated_at")
            ts = updated_at.timestamp() if updated_at is not None and hasattr(updated_at, "timestamp") else 0.0
            sessions.append(
                Session(
                    id=doc.id,
                    app_name=d.get("app_name", app_name),
                    user_id=d.get("uid", ""),
                    state={},
                    events=[],
                    last_update_time=ts,
                )
            )
        sessions.sort(key=lambda s: s.last_update_time)
        return ListSessionsResponse(sessions=sessions)


    async def delete_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> None:
        doc_ref = self._collection.document(session_id)
        doc_snap = doc_ref.get()
        if not doc_snap.exists:
            return
        data = doc_snap.to_dict() or {}
        if data.get("uid") != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to delete this chat session.",
            )

        # Delete both subcollections first, then the parent session document
        self._delete_subcollection(doc_ref, ADK_EVENTS_SUBCOLLECTION)
        self._delete_subcollection(doc_ref, UI_MESSAGES_SUBCOLLECTION)
        doc_ref.delete()


    async def append_event(self, session: Session, event: Event) -> Event:
        event = await super().append_event(session, event)
        if event.partial:
            return event

        doc_ref = self._collection.document(session.id)
        event_id = event.id or str(uuid4())
        serialized_event = event.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

        # Write the new event document to `chat_sessions/{sid}/adk_events/{event_id}`
        doc_ref.collection(ADK_EVENTS_SUBCOLLECTION).document(event_id).set(
            serialized_event
        )

        # Update parent session state & timestamp
        clean_state = self._clean_state_for_storage(session.state)
        doc_ref.update(
            {
                "state": clean_state,
                "selected_video_ids": clean_state.get("selected_video_ids", []),
                "updated_at": get_utc_now(),
            }
        )
        return event

    ############################################################
    # UI Session & Message Helpers (Used by FastAPI Routes)
    ############################################################

    def get_verified_session_doc(
        self, session_id: str, uid: str
    ) -> tuple[firestore.DocumentReference, dict[str, Any]]:
        """Fetches the parent session doc and verifies ownership by `uid`."""
        doc_ref = self._collection.document(session_id)
        doc_snap = doc_ref.get()
        if not doc_snap.exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat session '{session_id}' not found.",
            )
        data = doc_snap.to_dict() or {}
        if data.get("uid") != uid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this chat session.",
            )
        return doc_ref, data


    def list_user_sessions(self, uid: str) -> list[ChatSessionSummary]:
        """Returns lightweight session summaries for the left sidebar."""
        query = self._collection.where(filter=FieldFilter("uid", "==", uid)).order_by(
            "updated_at", direction=firestore.Query.DESCENDING
        )
        results: list[ChatSessionSummary] = []
        for doc in query.stream():
            d = doc.to_dict() or {}
            results.append(
                ChatSessionSummary(
                    id=doc.id,
                    uid=d.get("uid", uid),
                    title=d.get("title", "NEW CLIPPING LOG"),
                    selected_video_ids=d.get("selected_video_ids", []),
                    created_at=d.get("created_at") or get_utc_now(),
                    updated_at=d.get("updated_at") or get_utc_now(),
                )
            )
        return results


    def get_user_session_detail(self, session_id: str, uid: str) -> ChatSessionDetail:
        """
        Streams full UI messages from `chat_sessions/{sid}/messages` subcollection
        and refreshes GCS signed URLs on any ClipCandidates.
        """
        doc_ref, data = self.get_verified_session_doc(session_id, uid)

        msgs_query = doc_ref.collection(UI_MESSAGES_SUBCOLLECTION).order_by(
            "created_at", direction=firestore.Query.ASCENDING
        )

        signed_url_cache: dict[str, str] = {}
        messages: list[ChatMessageModel] = []

        for msg_doc in msgs_query.stream():
            raw_msg = msg_doc.to_dict() or {}
            msg = ChatMessageModel.model_validate(raw_msg)
            for cand in msg.clip_candidates:
                if cand.video_id not in signed_url_cache:
                    vid_snap = (
                        db.collection(LFVIDEO_COLLECTION_NAME)
                        .document(cand.video_id)
                        .get()
                    )
                    if vid_snap.exists:
                        vid_data = vid_snap.to_dict() or {}
                        if vid_data.get("uid") == uid and vid_data.get("gcs_uri"):
                            try:
                                signed_url_cache[cand.video_id] = (
                                    generate_video_signed_read_url(vid_data["gcs_uri"])
                                )
                            except Exception:
                                signed_url_cache[cand.video_id] = ""
                if signed_url_cache.get(cand.video_id):
                    cand.preview_url = signed_url_cache[cand.video_id]
            messages.append(msg)

        return ChatSessionDetail(
            id=session_id,
            uid=uid,
            title=data.get("title", "NEW CLIPPING LOG"),
            selected_video_ids=data.get("selected_video_ids", []),
            created_at=data.get("created_at") or get_utc_now(),
            updated_at=data.get("updated_at") or get_utc_now(),
            messages=messages,
        )


    def update_user_session(
        self,
        session_id: str,
        uid: str,
        title: str | None = None,
        selected_video_ids: list[str] | None = None,
    ) -> ChatSessionSummary:
        """Updates session title or pinned video scope (`selected_video_ids`)."""
        doc_ref, data = self.get_verified_session_doc(session_id, uid)
        now = get_utc_now()
        updates: dict[str, Any] = {"updated_at": now}

        if title is not None:
            updates["title"] = title.strip() or "NEW CLIPPING LOG"
        if selected_video_ids is not None:
            updates["selected_video_ids"] = selected_video_ids
            state = data.get("state") or {}
            state["selected_video_ids"] = selected_video_ids
            updates["state"] = state

        doc_ref.update(updates)
        merged = {**data, **updates}
        return ChatSessionSummary(
            id=session_id,
            uid=uid,
            title=merged.get("title", "NEW CLIPPING LOG"),
            selected_video_ids=merged.get("selected_video_ids", []),
            created_at=merged.get("created_at") or now,
            updated_at=now,
        )


    def append_ui_message(
        self,
        session_id: str,
        uid: str,
        message: ChatMessageModel,
        auto_title_from_user_text: str | None = None,
    ) -> None:
        """Writes a ChatMessageModel document into `chat_sessions/{sid}/messages/{msg_id}`."""
        doc_ref, data = self.get_verified_session_doc(session_id, uid)

        # 1. Write the UI message (with full trace & clip_candidates) to the subcollection
        msg_id = message.id or str(uuid4())
        doc_ref.collection(UI_MESSAGES_SUBCOLLECTION).document(msg_id).set(
            message.model_dump(mode="json")
        )

        # 2. Update parent session metadata (and auto-title on first message)
        msg_count = int(data.get("message_count", 0)) + 1
        updates: dict[str, Any] = {
            "message_count": msg_count,
            "updated_at": get_utc_now(),
        }

        current_title = data.get("title", "NEW CLIPPING LOG")
        if (
            auto_title_from_user_text
            and current_title == "NEW CLIPPING LOG"
            and msg_count <= 2
        ):
            cleaned = " ".join(auto_title_from_user_text.strip().split())
            updates["title"] = (cleaned[:42] + "...") if len(cleaned) > 45 else cleaned

        doc_ref.update(updates)

# Singleton instance for routes
firestore_session_service = FirestoreSessionService()