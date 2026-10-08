# agent/agent.py
# ADK 2.0 Multi-Agent Setup: Root Orchestrator + Single-Turn Clip Editor Sub-Agent

# Imports
from dotenv import load_dotenv
from google.adk import Agent
from google.genai import types
from pydantic import BaseModel, Field
import os

# Custom Prompt Imports
from agent.prompts.clip_editor_agent import get_clip_editor_agent_prompt
from agent.prompts.orchestrator_agent import get_orchestrator_agent_prompt

# Custom Tool Imports
from agent.tools.get_expanded_window import get_expanded_segment_window
from agent.tools.list_available_videos import list_available_videos
from agent.tools.propose_clip_candidate import propose_clip_candidate
from agent.tools.search_video_segments import search_video_segments

# Load ENV Vars
load_dotenv()

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