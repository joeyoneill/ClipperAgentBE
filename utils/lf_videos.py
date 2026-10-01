# Helper Utils for Long-form Video Routes File

# Imports
from dotenv import load_dotenv
from fastapi import HTTPException, status
import os

# Custom Dependencies
from db import db

# Load Env Vars
load_dotenv()
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]

################################################################
# Allowed LF Video File Upload Types
################################################################

ALLOWED_VIDEO_TYPES = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-msvideo": "avi",
    "video/x-matroska": "mkv"
}

################################################################
# Helper: Fetch & Authorize Video Doc
################################################################

def get_user_video_doc(video_id: str, uid: str):
    """
    Fetches the Firestore doc and verifies it exists and belongs to the user.
    """
    # Try and get Doc Snapshot
    try:
        doc_ref = db.collection(LFVIDEO_COLLECTION_NAME).document(video_id)
        doc_snap = doc_ref.get()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {e}"
        )
    if not doc_snap.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video record '{video_id}' not found."
        )

    # Get Video Data
    video_data = doc_snap.to_dict()
    if video_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video record '{video_id}' not found."
        )

    # Check if requesting User owns and return
    if video_data.get("uid") != uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this video."
        )
    return doc_ref, video_data