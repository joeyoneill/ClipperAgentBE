# Helper Utils for Long-form Video Routes File

# Imports
from datetime import timedelta
from dotenv import load_dotenv
from fastapi import HTTPException, status
from google.auth import default as google_auth_default
from google.auth.transport import requests as google_requests
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
GCS_ROOT_BUCKET_NAME = os.environ["GCS_ROOT_BUCKET_NAME"]
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
    bucket_prefix = f"gs://{GCS_ROOT_BUCKET_NAME}/"
    if not gcs_uri.startswith(bucket_prefix):
        raise ValueError(f"Invalid GCS URI")

    blob_path = gcs_uri.removeprefix(bucket_prefix)
    bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
    blob = bucket.blob(blob_path)

    # Works directly when using a Service Account JSON key; falls back to IAM signBlob on Cloud Run
    credentials, _ = google_auth_default()
    if hasattr(credentials, "sign_bytes"):
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
        )
    # Refresh token for Cloud Run / compute credentials IAM signing
    auth_request = google_requests.Request()
    credentials.refresh(auth_request)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
        service_account_email=getattr(credentials, "service_account_email", None),
        access_token=credentials.token,
    )