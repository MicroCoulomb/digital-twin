import json
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI, OpenAIError
from pydantic import BaseModel

from context import prompt

# Load environment variables
load_dotenv()

app = FastAPI()

# Configure CORS
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# AI provider configuration
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
DEFAULT_AWS_REGION = os.getenv("DEFAULT_AWS_REGION", "us-east-1")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
bedrock_client = None

# Memory storage configuration
USE_S3 = os.getenv("USE_S3", "false").lower() == "true"
S3_BUCKET = os.getenv("S3_BUCKET", "")
MEMORY_DIR = os.getenv("MEMORY_DIR", "../memory")

# Initialize S3 client if needed
if USE_S3:
    s3_client = boto3.client("s3")


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str


class Message(BaseModel):
    role: str
    content: str
    timestamp: str


def get_active_model() -> str:
    if AI_PROVIDER == "bedrock":
        return BEDROCK_MODEL_ID
    return OPENAI_MODEL


def get_bedrock_client():
    global bedrock_client
    if bedrock_client is None:
        bedrock_client = boto3.client(
            service_name="bedrock-runtime",
            region_name=DEFAULT_AWS_REGION,
        )
    return bedrock_client


def get_memory_path(session_id: str) -> str:
    return f"{session_id}.json"


def load_conversation(session_id: str) -> List[Dict]:
    """Load conversation history from storage."""
    if USE_S3:
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET, Key=get_memory_path(session_id))
            return json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return []
            raise

    file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as file:
            return json.load(file)
    return []


def save_conversation(session_id: str, messages: List[Dict]):
    """Save conversation history to storage."""
    if USE_S3:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=get_memory_path(session_id),
            Body=json.dumps(messages, indent=2),
            ContentType="application/json",
        )
        return

    os.makedirs(MEMORY_DIR, exist_ok=True)
    file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(messages, file, indent=2)


def build_openai_input(conversation: List[Dict], user_message: str) -> List[Dict]:
    items = []
    for msg in conversation[-50:]:
        role = msg.get("role", "user")
        if role not in {"user", "assistant"}:
            continue

        items.append(
            {
                "role": role,
                "content": msg.get("content", ""),
            }
        )

    items.append(
        {
            "role": "user",
            "content": user_message,
        }
    )
    return items


def call_openai(conversation: List[Dict], user_message: str) -> str:
    """Call OpenAI with conversation history."""
    if openai_client is None:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY is required when AI_PROVIDER=openai",
        )

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            instructions=prompt(),
            input=build_openai_input(conversation, user_message),
            max_output_tokens=2000,
            temperature=0.7,
            top_p=0.9,
        )
    except OpenAIError as exc:
        print(f"OpenAI error: {exc}")
        raise HTTPException(status_code=502, detail=f"OpenAI error: {str(exc)}") from exc

    if not response.output_text:
        raise HTTPException(status_code=502, detail="OpenAI returned an empty response")

    return response.output_text


def call_bedrock(conversation: List[Dict], user_message: str) -> str:
    """Call AWS Bedrock with conversation history."""
    messages = []

    for msg in conversation[-50:]:
        messages.append(
            {
                "role": msg["role"],
                "content": [{"text": msg["content"]}],
            }
        )

    messages.append(
        {
            "role": "user",
            "content": [{"text": user_message}],
        }
    )

    try:
        response = get_bedrock_client().converse(
            modelId=BEDROCK_MODEL_ID,
            system=[{"text": prompt()}],
            messages=messages,
            inferenceConfig={"maxTokens": 2000, "temperature": 0.7, "topP": 0.9},
        )
        return response["output"]["message"]["content"][0]["text"]
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ValidationException":
            print(f"Bedrock validation error: {exc}")
            raise HTTPException(status_code=400, detail="Invalid message format for Bedrock")
        if error_code == "AccessDeniedException":
            print(f"Bedrock access denied: {exc}")
            raise HTTPException(status_code=403, detail="Access denied to Bedrock model")

        print(f"Bedrock error: {exc}")
        raise HTTPException(status_code=500, detail=f"Bedrock error: {str(exc)}")


def generate_response(conversation: List[Dict], user_message: str) -> str:
    if AI_PROVIDER == "bedrock":
        return call_bedrock(conversation, user_message)
    if AI_PROVIDER == "openai":
        return call_openai(conversation, user_message)

    raise HTTPException(
        status_code=500,
        detail=f"Unsupported AI_PROVIDER '{AI_PROVIDER}'. Expected 'openai' or 'bedrock'.",
    )


@app.get("/")
async def root():
    return {
        "message": "AI Digital Twin API",
        "memory_enabled": True,
        "storage": "S3" if USE_S3 else "local",
        "ai_provider": AI_PROVIDER,
        "ai_model": get_active_model(),
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "use_s3": USE_S3,
        "ai_provider": AI_PROVIDER,
        "ai_model": get_active_model(),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        session_id = request.session_id or str(uuid.uuid4())
        conversation = load_conversation(session_id)
        assistant_response = generate_response(conversation, request.message)

        conversation.append(
            {"role": "user", "content": request.message, "timestamp": datetime.now().isoformat()}
        )
        conversation.append(
            {
                "role": "assistant",
                "content": assistant_response,
                "timestamp": datetime.now().isoformat(),
            }
        )

        save_conversation(session_id, conversation)
        return ChatResponse(response=assistant_response, session_id=session_id)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Error in chat endpoint: {str(exc)}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/conversation/{session_id}")
async def get_conversation(session_id: str):
    """Retrieve conversation history."""
    try:
        conversation = load_conversation(session_id)
        return {"session_id": session_id, "messages": conversation}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
