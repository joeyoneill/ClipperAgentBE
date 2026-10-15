# tests/conftest.py
# Global Pytest Fixtures & Offline GCP SDK Isolation

import os
from typing import Any, cast
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv
import pytest

# 1. Load environment variables strictly from .env (no hardcoded fallback defaults)
load_dotenv()

REQUIRED_ENV_VARS = [
    "GCP_PROJECT_ID",
    "GCP_REGION",
    "FIRESTORE_DB_NAME",
    "LFVIDEO_COLLECTION_NAME",
    "LFVIDEO_SEGMENT_COLLECTION_NAME",
    "CHAT_SESSION_COLLECTION_NAME",
    "SFVIDEO_COLLECTION_NAME",
    "GCS_ROOT_BUCKET_NAME",
    "VIDEO_PROCESSOR_JOB_NAME",
    "EMBEDDING_MODEL_NAME",
    "ORCHESTRATOR_MODEL_NAME",
    "CLIPPER_SUBAGENT_MODEL_NAME",
    "MODEL_ARMOR_TEMPLATE_NAME",
]
for _var in REQUIRED_ENV_VARS:
    if not os.environ.get(_var):
        raise RuntimeError(
            f"Missing required environment variable '{_var}'. Ensure .env is present before running pytest."
        )


# 2. Patch GCP client constructors before application singletons are instantiated
def _make_mock_firestore_client(*args: Any, **kwargs: Any) -> MagicMock:
    mock_db = MagicMock()
    mock_db._database = kwargs.get("database", os.environ["FIRESTORE_DB_NAME"])
    return mock_db


_firestore_patch = patch(
    "google.cloud.firestore.Client", side_effect=_make_mock_firestore_client
)
_firestore_async_patch = patch(
    "google.cloud.firestore.AsyncClient", return_value=MagicMock()
)
_storage_patch = patch("google.cloud.storage.Client", return_value=MagicMock())
_run_jobs_patch = patch("google.cloud.run_v2.JobsClient", return_value=MagicMock())
_genai_patch = patch("google.genai.Client", return_value=MagicMock())
_secret_patch = patch(
    "google.cloud.secretmanager.SecretManagerServiceClient",
    side_effect=RuntimeError("Secret Manager disabled in offline unit tests"),
)
_trace_patch = patch(
    "google.cloud.trace_v2.TraceServiceClient", return_value=MagicMock()
)

_firestore_patch.start()
_firestore_async_patch.start()
_storage_patch.start()
_run_jobs_patch.start()
_genai_patch.start()
_secret_patch.start()
_trace_patch.start()

# Safe to import application modules now
from fastapi.testclient import TestClient  # noqa: E402
import db as db_module  # noqa: E402
import storage as storage_module  # noqa: E402
from main import app  # noqa: E402
from utils.auth import UserInfo, get_current_user  # noqa: E402

# Explicitly typed MagicMock aliases so IDE type-checkers (Pylance/Pyright)
# recognize `.reset_mock`, `.return_value`, and `.side_effect` on mocked singletons.
mock_db: MagicMock = cast(MagicMock, db_module.db)
mock_gcs: MagicMock = cast(MagicMock, storage_module.gcs_client)


################################################################
# Helper Factories for Mock Firestore & ToolContext Objects
################################################################


def make_doc_snap(
    doc_id: str,
    data: dict[str, Any] | None,
    exists: bool = True,
) -> MagicMock:
    """Builds a mock Firestore DocumentSnapshot."""
    snap = MagicMock()
    snap.id = doc_id
    snap.exists = exists
    snap.to_dict.return_value = data
    snap.reference = MagicMock()
    snap.reference.id = doc_id
    return snap


def make_tool_context(
    user_id: str = "user_123",
    state: dict[str, Any] | None = None,
) -> MagicMock:
    """Builds a mock ADK ToolContext with `.user_id` and `.state`."""
    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.state = state if state is not None else {}
    return ctx


################################################################
# Shared Pytest Fixtures
################################################################


@pytest.fixture
def mock_user() -> UserInfo:
    """Default authenticated test user."""
    return UserInfo(
        uid="user_123",
        email="creator@example.com",
        claims={"sub": "user_123", "email": "creator@example.com"},
    )


@pytest.fixture
def other_user() -> UserInfo:
    """Secondary user for testing 403 ownership isolation."""
    return UserInfo(
        uid="user_999",
        email="other@example.com",
        claims={"sub": "user_999", "email": "other@example.com"},
    )


@pytest.fixture(autouse=True)
def reset_global_mocks() -> Any:
    """Resets global `db` and `gcs_client` mocks before each test."""
    mock_db.reset_mock(return_value=True, side_effect=True)
    mock_gcs.reset_mock(return_value=True, side_effect=True)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(mock_user: UserInfo) -> TestClient:
    """Authenticated FastAPI TestClient with `get_current_user` dependency override."""
    app.dependency_overrides[get_current_user] = lambda: mock_user
    return TestClient(app)


@pytest.fixture
def unauth_client() -> TestClient:
    """Unauthenticated FastAPI TestClient without auth overrides."""
    app.dependency_overrides.clear()
    return TestClient(app)
