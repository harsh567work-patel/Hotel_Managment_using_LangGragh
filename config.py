"""
config.py — Central configuration for SpiceRoute Kitchen food ordering system.
All environment variables, model names, paths, and thresholds are configured here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"

# ── LLM Models (Groq) ────────────────────────────────────────────────────────
# Default to high-performance and fast models available on the Groq account
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FAST_MODEL = os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_api_key")

# ── Qdrant Configuration ─────────────────────────────────────────────────────
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or os.getenv("QDRANT_api_key")

MENU_COLLECTION = "restaurant_menu"
QA_COLLECTION = "qa_golden_dataset"
VECTOR_SIZE = 384  # BAAI/bge-small-en-v1.5 dimension via FastEmbed
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# Semantic match score threshold for full-menu dish resolution (cosine similarity 0-1)
DISH_MATCH_THRESHOLD = 0.65

# ── Delivery & Business Logic ────────────────────────────────────────────────
DELIVERY_BUFFER_MINUTES = 15
MAX_FALLBACKS = 3
MAX_BACKTRACKS = 2

# Serviceable cities for delivery
SERVICEABLE_CITIES = ["Bangalore", "Bengaluru"]
