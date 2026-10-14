# agent/agent.py
# ADK 2.0 Multi-Agent Setup: Root Orchestrator + Single-Turn Clip Editor Sub-Agent

# Imports
from dotenv import load_dotenv
from google.adk import Agent
from google.adk.apps import App
from google.adk.integrations.model_armor import ModelArmorConfig, ModelArmorPlugin
from google.genai import types
from pydantic import BaseModel, Field
import os

# Custom Prompt Imports
from agent.prompts.clip_editor_agent import get_clip_editor_agent_prompt
from agent.prompts.orchestrator_agent import get_orchestrator_agent_prompt
from utils.chat_sessions import CHAT_APP_NAME
from utils.secret_manager import load_secrets_into_env

# Custom Tool Imports
from agent.tools.get_expanded_window import get_expanded_segment_window
from agent.tools.list_available_videos import list_available_videos
from agent.tools.propose_clip_candidate import propose_clip_candidate
from agent.tools.search_video_segments import search_video_segments

# Load ENV Vars
load_dotenv()
load_secrets_into_env()

################################################################
# Schema-Validated Delegation Input for Clip Editor Sub-Agent
################################################################

class ClipEditorInput(BaseModel):
    video_id: str = Field(
        description="The exact video_id returned by search_video_segments."
    )
    center_segment_index: int = Field(
        description="The integer segment_index of the 30-second anchor segment to expand around."
    )
    clip_goal: str = Field(
        description="Instructions on what topic, moment, or duration the clip should capture."
    )

################################################################
# Sub-Agent: Single-Turn Precision Clip Boundary Editor
################################################################

clip_editor_agent = Agent(
    name="clip_editor_agent",
    model=os.environ["CLIPPER_SUBAGENT_MODEL_NAME"],
    mode="single_turn",
    input_schema=ClipEditorInput,
    description=(
        "Precision video-editing sub-agent. Given a video_id, a center_segment_index, "
        "and a clip_goal, it loads the +-2 chunk expanded word-level timeline, selects "
        "natural sentence start/stop timestamps, and proposes a ClipCandidate card."
    ),
    instruction=get_clip_editor_agent_prompt(),
    tools=[
        get_expanded_segment_window,
        propose_clip_candidate,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.1,
        thinking_config=types.ThinkingConfig(
            include_thoughts=True,
        ),
    ),
)

################################################################
# Root Agent: Conversational RAG & Studio Orchestrator
################################################################

orchestrator_agent = Agent(
    name="orchestrator_agent",
    model=os.environ["ORCHESTRATOR_MODEL_NAME"],
    description="Main conversational RAG and short-form video clipping studio agent.",
    instruction=get_orchestrator_agent_prompt(),
    tools=[
        list_available_videos,
        search_video_segments,
    ],
    sub_agents=[
        clip_editor_agent,
    ],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.3,
        thinking_config=types.ThinkingConfig(
            include_thoughts=True,
        ),
    ),
)

# Alias expected by `adk web` / `adk run` CLI tools
root_agent = orchestrator_agent

################################################################
# ADK App Container with Google Cloud Model Armor Guardrail
################################################################

def _build_plugins() -> list:
    template_name = os.getenv("MODEL_ARMOR_TEMPLATE_NAME", "").strip()
    if not template_name:
        return []
    return [
        ModelArmorPlugin(
            config=ModelArmorConfig(
                prompt_template_name=template_name,
                response_template_name=template_name,
                input_blocked_message=(
                    "⚠️ [SECURITY GUARDRAIL] Your request was blocked by Google Cloud "
                    "Model Armor policy (prompt injection or unsafe content detected). "
                    "Please ask a question about your uploaded videos or request a short-form clip."
                ),
                output_blocked_message=(
                    "⚠️ [SECURITY GUARDRAIL] The agent response was withheld by Google Cloud "
                    "Model Armor safety filters."
                ),
                block_on_screening_failure=False,
            )
        )
    ]

clipping_app = App(
    name=CHAT_APP_NAME,
    root_agent=root_agent,
    plugins=_build_plugins(),
)