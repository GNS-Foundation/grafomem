import asyncio
from src.aml.cloud.llm_registry import LLMRegistry
from src.aml.cloud.orchestrator import LLMRequest
import uuid
import os

# Create an empty sqlite DB so we can instantiate the registry easily without postgres
# Wait, LLMRegistry uses psycopg which means it requires Postgres!
