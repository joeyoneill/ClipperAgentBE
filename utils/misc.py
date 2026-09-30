# Random MISC Utils

# imports
from datetime import datetime, timezone

# Gets Current UTC Timestamp
def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)