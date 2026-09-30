# Imports
from google.adk import Agent

# Simple ADK agent for stream testing
orchestrator_agent = Agent(
    name="orchestrator_agent",
    model="gemini-3.5-flash",
    instruction="You are a helpful support agent. Execute tools as needed. Or just asnswer questions to the best of your ability.",
    tools=[]  # Register your tools here
)