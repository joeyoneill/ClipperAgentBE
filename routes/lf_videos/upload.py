# Long-Form Video Upload Endpoint(s)

# imports
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status
)
import os

# Custom Dependencies
from db import db
from models.lf_videos import (
    LFUploadFailedRequest,
    LFUploadRequest,
    LFUploadResponse,
    LFVideo
)
from storage import gcs_client
from utils.auth import UserInfo, get_current_user
from utils.lf_videos import (
    ALLOWED_VIDEO_TYPES,
    get_user_video_doc
)
from utils.misc import get_utc_now

# Init Router
router = APIRouter(prefix="/upload", tags=["Video Upload"])

# Load Env Vars
load_dotenv()
GCS_ROOT_BUCKET_NAME = os.environ["GCS_ROOT_BUCKET_NAME"]
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]

################################################################
# POST: /api/lf_videos/upload/init
################################################################

@router.post("/init", response_model=LFUploadResponse, status_code=status.HTTP_201_CREATED)
async def init_lf_video_upload(
    request: Request,
    payload: LFUploadRequest,
    user: UserInfo = Depends(get_current_user)
) -> LFUploadResponse:
    """
    1. Validates the video content type against an allowed list.
    2. Pre-generates a Firestore Document to secure a unique video_id.
    3. Builds the structured GCS blob path: <uid>/lfvideos/<video_id>/original.<ext>
    4. Persists the initial 'PENDING' record in Firestore.
    5. Negotiates a Resumable Upload Session URL directly with Google Cloud Storage.
    6. Returns the session URL and metadata to the React frontend.
    """

    # Check Content Type
    content_type = payload.content_type.strip().lower()
    if content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type. Allowed types: {list(ALLOWED_VIDEO_TYPES.keys())}"
        )
    safe_extension = ALLOWED_VIDEO_TYPES[content_type]

    # Pregenerate Firestore Doc
    try:
        doc_ref = db.collection(LFVIDEO_COLLECTION_NAME).document()
        video_id = doc_ref.id
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to connect to database..."
        )

    # Build GCS Path
    blob_path = f"{user.uid}/lf_videos/{video_id}/original.{safe_extension}"
    gcs_uri = f"gs://{GCS_ROOT_BUCKET_NAME}/{blob_path}"

    # Upload Initial PENDING Doc to Firestore
    try:
        doc_ref.set(
            LFVideo(
                uid = user.uid,
                filename = payload.filename,
                gcs_uri = gcs_uri,
                status = "PENDING",
                content_type = content_type,
                file_size_bytes = payload.file_size_bytes,
                duration_seconds = payload.duration_seconds
            ).model_dump(exclude={"id"})
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create DB record: {e}"
        )

    # Try and Get Resumable Upload Session URL from GCS
    try:
        bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
        blob = bucket.blob(blob_path)
        origin = request.headers.get("origin", "*")
        raw_upload_url = blob.create_resumable_upload_session(
            content_type=content_type,
            origin=origin
        )
        if not raw_upload_url:
            raise ValueError("GCS returned an empty or invalid upload session URL.")
        upload_url = str(raw_upload_url)
    except Exception as e:
        doc_ref.delete() # delete the pending Firestore document if failed
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate GCS resumable upload session: {str(e)}"
        )

    # Return information to FE
    return LFUploadResponse(
        video_id=video_id,
        upload_url=upload_url,
        gcs_uri=gcs_uri,
        blob_path=blob_path
    )

################################################################
# POST: /api/lf_videos/upload/{video_id}/complete
################################################################

@router.post("/{vid}/complete", response_model=LFVideo, status_code=status.HTTP_200_OK)
async def completed_upload(
    vid: str,
    user: UserInfo = Depends(get_current_user)
) -> LFVideo:
    """
    1. Verifies the video document exists in Firestore and belongs to the user.
    2. Checks GCS to confirm the blob was actually finalized.
    3. Updates the Firestore document status to 'SUCCESSFUL' (or 'PROCESSING' later).
    """
    doc_ref, video_data = get_user_video_doc(vid, user.uid)

    # Extract blob_path from stored gcs_uri: gs://<bucket>/<blob_path>
    gcs_uri: str = video_data.get("gcs_uri", "")
    bucket_prefix = f"gs://{GCS_ROOT_BUCKET_NAME}/"
    if not gcs_uri.startswith(bucket_prefix):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored GCS URI is malformed."
        )
    blob_path = gcs_uri.removeprefix(bucket_prefix)

    # Verify the object actually landed in GCS
    try:
        bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
        blob = bucket.blob(blob_path)
        if not blob.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Video file not found in GCS. Resumable upload is not complete."
            )
        blob.reload()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to verify blob in GCS: {e}"
        )

    # Update Firestore state
    updates = {
        "status": "SUCCESSFUL",
        "is_complete": True,
        "error_msg": None,
        "file_size_bytes": blob.size or video_data.get("file_size_bytes"),
        "updated_at": get_utc_now(),
    }
    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update DB record: {e}"
        )
    return LFVideo(id=vid, **{**video_data, **updates})

################################################################
# POST: /api/lf_videos/upload/{video_id}/fail
################################################################

@router.post("/{vid}/failed", response_model=LFVideo, status_code=status.HTTP_200_OK)
async def failed_upload(
    vid: str,
    payload: LFUploadFailedRequest,
    user: UserInfo = Depends(get_current_user)
) -> LFVideo:
    """
    1. Verifies the video document exists in Firestore and belongs to the user.
    2. Prevents overwriting an already completed video.
    3. Marks the Firestore record as 'FAILED' and stores the error message.
    """
    doc_ref, video_data = get_user_video_doc(vid, user.uid)

    # Prevent overwriting completed uploaded videos
    if video_data.get("is_complete") or video_data.get("status") == "SUCCESSFUL":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot mark an already completed upload as failed."
        )

    # Firestore Update Dict
    updates = {
        "status": "FAILED",
        "is_complete": False,
        "error_msg": payload.error_msg,
        "updated_at": get_utc_now()
    }

    # Update Firestore Obj
    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update DB record: {e}"
        )
    return LFVideo(id=vid, **{**video_data, **updates})