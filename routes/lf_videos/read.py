# CRUD READ Endpoints

# Imports
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, status
from google.cloud.firestore_v1.base_query import FieldFilter
import os

# Custom Dependencies
from db import db
from models.lf_videos import LFVideo
from utils.auth import UserInfo, get_current_user
from utils.lf_videos import get_user_video_doc

# Init Router
router = APIRouter(tags=["LFVideo Read"])

# Load Env Vars
load_dotenv()
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]

################################################################
# GET: /api/lf_videos
################################################################

# Gets all of a user's LF video details
@router.get("/list", response_model=list[LFVideo], status_code=status.HTTP_200_OK)
def get_user_lf_videos(
    user: UserInfo = Depends(get_current_user)
) -> list[LFVideo]:
    """
    Fetches all LFVideo records belonging to the authenticated user,
    sorted newest first by created_at.
    """
    try:
        docs = (
            db.collection(LFVIDEO_COLLECTION_NAME)
            .where(filter=FieldFilter("uid", "==", user.uid))
            .stream()
        )
        videos: list[LFVideo] = []
        for doc in docs:
            data = doc.to_dict()
            if data is not None:
                videos.append(LFVideo.model_validate({"id": doc.id, **data}))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch videos from database: {e}"
        )
    
    # Sort newest first in memory (avoids needing a Firestore composite index)
    videos.sort(key=lambda v: v.created_at, reverse=True)
    return videos

################################################################
# GET: /api/lf_videos/{vid}
################################################################

# Gets an individual LF video details
@router.get("/{vid}", response_model=LFVideo, status_code=status.HTTP_200_OK)
def get_lf_video(
    vid: str,
    user: UserInfo = Depends(get_current_user)
) -> LFVideo:
    """
    Fetches a single LFVideo record by ID after verifying ownership.
    """
    _, video_data = get_user_video_doc(vid, user.uid)
    return LFVideo(id=vid, **video_data)