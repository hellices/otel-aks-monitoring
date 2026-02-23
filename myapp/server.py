"""AG-UI server with Azure AI Foundry via Service Connector (Workload Identity)."""

import os

from agent_framework import Agent
from agent_framework.azure import AzureOpenAIChatClient
from agent_framework_ag_ui import add_agent_framework_fastapi_endpoint
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from opentelemetry import metrics
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

# Instrument OpenAI SDK for gen_ai.* metrics
OpenAIInstrumentor().instrument()

# Custom metric: count agent API requests
meter = metrics.get_meter("agui.server", "1.0.0")
agent_request_counter = meter.create_counter(
    "agui.agent.request.count",
    description="Total number of AG-UI agent requests",
    unit="1",
)

# Service Connector injects these env vars from sc-aifconnection-secret:
#   AZURE_AISERVICES_OPENAI_BASE  → https://aif-contoso-krc-01.openai.azure.com/
#   AZURE_AISERVICES_CLIENTID     → UAMI client ID (used by Workload Identity)
endpoint = os.environ.get("AZURE_AISERVICES_OPENAI_BASE")
deployment_name = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5.2-chat")

if not endpoint:
    raise ValueError("AZURE_AISERVICES_OPENAI_BASE environment variable is required")

# DefaultAzureCredential picks up Workload Identity automatically in AKS
chat_client = AzureOpenAIChatClient(
    credential=DefaultAzureCredential(),
    endpoint=endpoint.rstrip("/"),
    deployment_name=deployment_name,
)

agent = Agent(
    chat_client,
    instructions="You are a helpful assistant running on AKS with Azure AI Foundry.",
    name="AGUIAssistant",
)

app = FastAPI(title="AG-UI Server")
add_agent_framework_fastapi_endpoint(app, agent, "/api/agent")


@app.middleware("http")
async def track_agent_requests(request: Request, call_next):
    if request.url.path == "/api/agent":
        agent_request_counter.add(1, {"http.method": request.method})
    return await call_next(request)


@app.get("/")
async def root():
    return RedirectResponse("/chat/")


app.mount("/chat", StaticFiles(directory="static", html=True), name="chat")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
