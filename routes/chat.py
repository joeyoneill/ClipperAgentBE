
# Imports
from dotenv import load_dotenv
from fastapi import APIRouter
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part
from uuid import uuid4

# Custom Dependencies
from agent.agent import orchestrator_agent
from models.chat import ChatRequest

# Load Env Vars
load_dotenv()

# Init API Router
router = APIRouter(prefix="/chat")

# Temporary Local Sessions
session_service = InMemorySessionService()

# Main Orchestrator Non-streamable Response Endpoint
@router.post("", tags=["Chat"])
async def agent_response(payload: ChatRequest):
    app_name = "testApp"

    # check for session
    session = await session_service.get_session(
        app_name=app_name,
        user_id=payload.user_id,
        session_id=payload.session_id
    )
    if not session:
        await session_service.create_session(
            app_name=app_name,
            user_id=payload.user_id,
            session_id=payload.session_id
        )
        session = await session_service.get_session(
            app_name=app_name,
            user_id=payload.user_id,
            session_id=payload.session_id
        )

    # init runner
    runner = Runner(
        app_name=app_name,
        agent=orchestrator_agent,
        session_service=session_service
    )

    # craft message
    user_msg = Content(
        role="User",
        parts=[
            Part(text=payload.query)
        ]
    )

    # Get Agent Response
    response = runner.run(
        session_id=payload.session_id,
        user_id=payload.user_id,
        new_message=user_msg
    )

    # return agent response
    return {
        "session": session,
        "response": response
    }

# Main Orchestrator Websocket Connection
@router.websocket("/stream")
def agent_stream():
    pass