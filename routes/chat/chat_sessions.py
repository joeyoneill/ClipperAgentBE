# Chat Session Management Endpoints

# Imports
from fastapi import APIRouter, Depends, HTTPException, Response, status

# Custom Dependencies
from utils.auth import (
    get_current_user,
    UserInfo
)
from models.chat import (
    CreateChatSessionRequest,
    ChatSessionDetail,
    ChatSessionSummary,
    UpdateChatSessionRequest,
)
from utils.chat_sessions import (
    CHAT_APP_NAME,
    get_verified_adk_session,
    session_service,
    session_to_chat_detail,
    session_to_chat_summary,
    update_session_state_fields,
    validate_owned_video_ids
)
from utils.misc import get_utc_now

# Init Router
router = APIRouter(prefix="/sessions", tags=["Chat Sessions"])

################################################################
# GET: /api/chat/sessions
################################################################

@router.get("", response_model=list[ChatSessionSummary], status_code=status.HTTP_200_OK)
async def list_chat_sessions(
    user: UserInfo = Depends(get_current_user),
) -> list[ChatSessionSummary]:
    """
    1, lists all chat sessions belonging to the authenticated user via ADK FirestoreSessionService.
    2. Converts each ADK Session into a lightweight ChatSessionSummary (no message/event payload).
    3. Reverses ADK's oldest-first ordering so the most recently active session is first.
    """
    try:
        resp = await session_service.list_sessions(
            app_name=CHAT_APP_NAME,
            user_id=user.uid,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list chat sessions: {e}",
        )
    return [
        session_to_chat_summary(session)
        for session in reversed(resp.sessions)
    ]

################################################################
# POST: /api/chat/sessions
################################################################

@router.post("", response_model=ChatSessionSummary, status_code=status.HTTP_201_CREATED)
async def create_chat_session(
    payload: CreateChatSessionRequest,
    user: UserInfo = Depends(get_current_user),
) -> ChatSessionSummary:
    """
    1. Validates any initially pinned `selected_video_ids` against the user's Vault.
    2. Creates a new ADK Session in Firestore initialized with `title` and `selected_video_ids`.
    3. Returns the newly created ChatSessionSummary to the React frontend.
    """
    # validate videos
    verified_vids = validate_owned_video_ids(payload.selected_video_ids, user.uid)
    
    # build state
    clean_title = (payload.title or "").strip() or "NEW CLIPPING LOG"
    initial_state = {
        "title": clean_title,
        "selected_video_ids": verified_vids,
        "created_at": get_utc_now().isoformat(),
    }

    # create new adk session
    try:
        session = await session_service.create_session(
            app_name=CHAT_APP_NAME,
            user_id=user.uid,
            state=initial_state,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create chat session: {e}",
        )

    # return obj for FE
    return session_to_chat_summary(session)

################################################################
# GET: /api/chat/sessions/{session_id}
################################################################

@router.get("/{session_id}", response_model=ChatSessionDetail, status_code=status.HTTP_200_OK)
async def get_chat_session_detail(
    session_id: str,
    user: UserInfo = Depends(get_current_user),
) -> ChatSessionDetail:
    """
    Returns Chat Session detail by id.

    1. Verifies the chat session exists and belongs to the requesting user.
    2. Loads the full ADK event history from Firestore.
    3. Reconstructs UI messages, agent reasoning/tool traces, and re-signs ClipCandidate preview URLs.
    """
    session = await get_verified_adk_session(
        session_id=session_id,
        uid=user.uid,
        include_events=True,
    )

    try:
        return session_to_chat_detail(session)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reconstruct chat session history: {e}",
        )

################################################################
# PATCH: /api/chat/sessions/{session_id}
################################################################

@router.patch("/{session_id}", response_model=ChatSessionSummary, status_code=status.HTTP_200_OK)
async def update_chat_session(
    session_id: str,
    payload: UpdateChatSessionRequest,
    user: UserInfo = Depends(get_current_user),
) -> ChatSessionSummary:
    """
    Updates session info like title or pinned videos.

    1. Verifies the chat session exists and belongs to the user (without loading full event history).
    2. Validates any newly pinned `selected_video_ids` against the user's Vault.
    3. Appends a state-delta event via ADK FirestoreSessionService to update `title` / `selected_video_ids`.
    """
    session = await get_verified_adk_session(
        session_id=session_id,
        uid=user.uid,
        include_events=False,
    )

    updates: dict[str, object] = {}
    if payload.title is not None:
        updates["title"] = payload.title.strip() or "NEW CLIPPING LOG"

    if payload.selected_video_ids is not None:
        updates["selected_video_ids"] = validate_owned_video_ids(
            payload.selected_video_ids,
            user.uid,
        )

    if not updates:
        return session_to_chat_summary(session)

    try:
        updated_session = await update_session_state_fields(session, updates)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update chat session state: {e}",
        )

    return session_to_chat_summary(updated_session)

################################################################
# DELETE: /api/chat/sessions/{session_id}
################################################################

@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_session(
    session_id: str,
    user: UserInfo = Depends(get_current_user),
) -> Response:
    """
    Deletes an ADK session by ID
    
    1. Verifies the chat session exists and belongs to the requesting user.
    2. Deletes all persisted ADK events in the session's `events` subcollection and the session doc.
    """
    await get_verified_adk_session(
        session_id=session_id,
        uid=user.uid,
        include_events=False,
    )

    try:
        await session_service.delete_session(
            app_name=CHAT_APP_NAME,
            user_id=user.uid,
            session_id=session_id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete chat session: {e}",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)

