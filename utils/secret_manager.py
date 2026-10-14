# utils/secrets.py
# GCP Secret Manager Integration & Runtime Secret Resolver

# Imports
from dotenv import load_dotenv
from functools import lru_cache
from google.cloud import secretmanager
import logging
import os

# Load Env Vars
load_dotenv()
logger = logging.getLogger(__name__)

# List of secret names stored in GCP Secret Manager to auto-inject into os.environ
MANAGED_SECRET_KEYS = [
    "GEMINI_API_KEY",
]

################################################################
# GCP Secret Manager Functions
################################################################

@lru_cache(maxsize=64)
def get_secret(
    secret_id: str,
    default: str | None = None,
    version: str = "latest",
) -> str | None:
    """
    Fetches a secret from GCP Secret Manager (`projects/{GCP_PROJECT_ID}/secrets/{secret_id}/versions/latest`),
    falling back to `os.environ` if Secret Manager is unreachable.
    """
    project_id = os.getenv("GCP_PROJECT_ID", "").strip()
    if project_id:
        try:
            client = secretmanager.SecretManagerServiceClient()
            secret_path = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
            response = client.access_secret_version(request={"name": secret_path})
            payload = response.payload.data.decode("utf-8").strip()
            if payload:
                return payload
        except Exception as e:
            logger.debug(
                f"Could not fetch '{secret_id}' from Secret Manager ({e}); falling back to env."
            )
    env_val = os.getenv(secret_id)
    if env_val is not None and env_val.strip() != "":
        return env_val.strip()
    return default


def load_secrets_into_env(secret_keys: list[str] | None = None) -> None:
    """
    Pulls secrets from GCP Secret Manager and injects them directly into `os.environ`
    so Google ADK, GenAI SDK, and existing `os.environ[...]` calls work automatically.
    """
    keys_to_load = secret_keys or MANAGED_SECRET_KEYS
    for key in keys_to_load:
        val = get_secret(key)
        if val:
            os.environ[key] = val