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
MAX_ACTIVE_ORDERS = 5
MAX_FALLBACKS = 3
MAX_BACKTRACKS = 2

# Serviceable cities for delivery
SERVICEABLE_CITIES = ["Bangalore", "Bengaluru"]
RESTAURANT_LOCATION = "12, Food Street, Bangalore - 560001"

# ── Table Split Mode Configuration ──────────────────────────────────────────
TABLE_SPLIT_ENABLED = os.getenv("TABLE_SPLIT_ENABLED", "false").lower() in ("true", "1", "yes")
AI_PARSER_ENABLED = os.getenv("AI_PARSER_ENABLED", "false").lower() in ("true", "1", "yes")
TABLE_SPLIT_PARSER_MODEL = os.getenv("TABLE_SPLIT_PARSER_MODEL", GROQ_FAST_MODEL)
TABLE_SPLIT_PARSER_TIMEOUT_SECONDS = float(os.getenv("TABLE_SPLIT_PARSER_TIMEOUT_SECONDS", "4.0"))
TABLE_SPLIT_PARSER_MAX_RETRIES = int(os.getenv("TABLE_SPLIT_PARSER_MAX_RETRIES", "1"))
TABLE_SPLIT_PARSER_LOG_RETENTION_SECONDS = int(
    os.getenv("TABLE_SPLIT_PARSER_LOG_RETENTION_SECONDS", str(7 * 86400))
)
TABLE_SPLIT_SESSION_TTL_MINUTES = int(os.getenv("TABLE_SPLIT_SESSION_TTL_MINUTES", "120"))
TABLE_SPLIT_DEFAULT_EXPIRY_SECONDS = int(os.getenv("TABLE_SPLIT_DEFAULT_EXPIRY_SECONDS", "7200"))  # 2 hours
TABLE_SPLIT_MAX_EXPIRY_SECONDS = int(os.getenv("TABLE_SPLIT_MAX_EXPIRY_SECONDS", "86400"))     # 24 hours max
TABLE_SPLIT_RETENTION_SECONDS = int(os.getenv("TABLE_SPLIT_RETENTION_SECONDS", "86400"))       # 24h after expiry
TABLE_SPLIT_MAX_PARTICIPANTS = int(os.getenv("TABLE_SPLIT_MAX_PARTICIPANTS", "12"))
TABLE_SPLIT_ROOM_CODE_PREFIX = os.getenv("TABLE_SPLIT_ROOM_CODE_PREFIX", "TAB")
TABLE_SPLIT_ROOM_CODE_SUFFIX_LEN = int(os.getenv("TABLE_SPLIT_ROOM_CODE_SUFFIX_LEN", "4"))
RESTAURANT_GST_BPS = int(os.getenv("RESTAURANT_GST_BPS", "500"))  # 5.00% GST (basis points)
TABLE_SPLIT_DB_PATH = os.getenv("TABLE_SPLIT_DB_PATH", str(DATA_DIR / "table_split.db"))

