# Helper Utils for Long-form Video Routes File

# Imports
from dotenv import load_dotenv
from fastapi import HTTPException, status
from google.cloud import run_v2
import os

# Custom Dependencies
from db import db
from storage import gcs_client

# Load Env Vars
load_dotenv()
LFVIDEO_COLLECTION_NAME = os.environ["LFVIDEO_COLLECTION_NAME"]
GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]
GCP_REGION = os.environ["GCP_REGION"]
VIDEO_PROCESSOR_JOB_NAME = os.environ["VIDEO_PROCESSOR_JOB_NAME"]

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

################################################################
# Helper: Triggers Video Processing Cloud Run Job
################################################################

# Init Run Jobs Client
run_jobs_client = run_v2.JobsClient()


def trigger_video_processor_job(vid: str, uid: str) -> None:
    """
    Triggers an async Cloud Run Job execution passing --video-id and --uid.
    Returns immediately once Cloud Run queues the execution.
    """
    job_path = (
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/jobs/{VIDEO_PROCESSOR_JOB_NAME}"
    )
    request = run_v2.RunJobRequest(
        name=job_path,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    args=["--video-id", vid, "--uid", uid],
                )
            ]
        ),
    )
    run_jobs_client.run_job(request=request)

################################################################
# Helper: Generates GCS Read URL
################################################################

def generate_video_signed_read_url(
    gcs_uri: str,
    expiration_minutes: int = 60
) -> str:
    """
    Generates a v4 Signed GET URL for a gs://<bucket>/<blob_path> URI
    so the frontend <video> player can stream & seek inline clip previews.
    """
    return ""