# routes/sf_videos/save.py
# Short-Form Video (Reel Clip) Human-in-the-Loop Save Endpoint

# Imports
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from google.cloud.firestore_v1.base_query import FieldFilter
import os

# Custom Dependencies
from db import db
from models.sf_videos import (
    SaveClipRequest,
    SFVideo,
)
from utils.auth import UserInfo, get_current_user
from utils.lf_videos import get_user_video_doc
from utils.misc import get_utc_now

# Init Router
router = APIRouter(tags=["SFVideos (Reel)"])

# Load Env Vars
load_dotenv()
SFVIDEO_COLLECTION_NAME = os.environ["SFVIDEO_COLLECTION_NAME"]

################################################################
# POST: /api/sf_videos/save
################################################################

@router.post("/save", response_model=SFVideo, status_code=status.HTTP_201_CREATED)
def save_clip_to_reel(
    payload: SaveClipRequest,
    user: UserInfo = Depends(get_current_user),
) -> SFVideo:
    """
    Saves SF video clip to Firestore
    
    1. Verifies the parent long-form video exists in Firestore and belongs to the user.
    2. Validates start_sec and end_sec boundaries.
    3. Performs an idempotency check on (uid, candidate_id) to prevent duplicate saves.
    4. Persists a new 'PENDING_CUT' SFVideo record (with word-level timestamps) in Firestore.
    """
    
    # Verify parent LFVideo ownership
    _, lf_video_data = get_user_video_doc(payload.video_id, user.uid)
    if lf_video_data.get("status") != "SUCCESSFUL":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot save a clip from a video that has not finished processing.",
        )

    # Validate clip time boundaries
    start_sec = round(float(payload.start_sec), 2)
    end_sec = round(float(payload.end_sec), 2)
    if start_sec < 0 or end_sec <= start_sec:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid clip timestamps: start_sec ({start_sec}) must be >= 0 and < end_sec ({end_sec}).",
        )

    collection_ref = db.collection(SFVIDEO_COLLECTION_NAME)

    # Idempotency check: if this candidate_id was already saved by the user, return it
    if payload.candidate_id:
        existing_query = (
            collection_ref.where(filter=FieldFilter("uid", "==", user.uid))
            .where(filter=FieldFilter("candidate_id", "==", payload.candidate_id))
            .limit(1)
        )
        for existing_snap in existing_query.stream():
            existing_data = existing_snap.to_dict() or {}
            return SFVideo(id=existing_snap.id, **existing_data)

    # Create new SFVideo document in Firestore
    now = get_utc_now()
    sf_video = SFVideo(
        uid=user.uid,
        source_video_id=payload.video_id,
        source_filename=lf_video_data.get("filename", "untitled.mp4"),
        session_id=payload.session_id,
        candidate_id=payload.candidate_id,
        title=payload.title.strip() or "UNTITLED CLIP",
        start_sec=start_sec,
        end_sec=end_sec,
        duration_seconds=round(end_sec - start_sec, 2),
        transcript_text=payload.transcript_text,
        words=payload.words,
        rationale=payload.rationale,
        status="PENDING_CUT",
        is_complete=False,
        gcs_uri=None,
        error_msg=None,
        created_at=now,
        updated_at=now,
    )

    try:
        doc_ref = collection_ref.document()
        doc_ref.set(sf_video.model_dump(exclude={"id"}))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save clip to Reel database: {e}",
        )

    sf_video.id = doc_ref.id
    return sf_video