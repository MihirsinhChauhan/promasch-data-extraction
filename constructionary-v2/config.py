"""Environment and paths for constructionary-v2 (dotenv + defaults)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PACKAGE_DIR = Path(__file__).resolve().parent

# Promasch credentials
PROM_BASE_URL = os.getenv("PROM_BASE_URL", "https://gw.promasch.in")
PROM_USER = os.getenv("CONSTRUCTIONARY_USER", "")
PROM_PASSWORD = os.getenv("CONSTRUCTIONARY_PASSWORD", "")

# AWS — same env vars as vendor-bills
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_BUCKET_NAME = os.getenv("CONSTRUCTIONARY_BUCKET_NAME", "constructionary-images")

# S3 key prefix for part images
S3_PART_IMAGE_PREFIX = os.getenv("S3_PART_IMAGE_PREFIX", "constructionary/parts")

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.3"))
IMAGE_DOWNLOAD_TIMEOUT = float(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "60"))
IMAGE_MAX_BYTES = int(os.getenv("IMAGE_MAX_BYTES", str(25 * 1024 * 1024)))
DETAIL_RPC_TIMEOUT = float(os.getenv("DETAIL_RPC_TIMEOUT", "30"))
DETAIL_MAX_RETRIES = int(os.getenv("DETAIL_MAX_RETRIES", "3"))

# Default data dir when running from package root (overridden by CLI --data-dir)
DATA_DIR = Path(os.getenv("CONSTRUCTIONARY_V2_DATA", str(PACKAGE_DIR / "data")))

# Artifact paths (relative to DATA_DIR; overridable individually)
AUTH_STATE_FILE = DATA_DIR / "auth_state.json"
RPC_TEMPLATE_FILE = DATA_DIR / "rpc_template.json"
PARTS_INDEX_FILE = DATA_DIR / "parts_index.jsonl"
BULK_DUMPS_DIR = DATA_DIR / "bulk_dumps"
DETAIL_DUMPS_DIR = DATA_DIR / "detail_dumps"
PARTS_RECORDS_FILE = DATA_DIR / "parts_records.jsonl"
FAILED_IMAGES_FILE = DATA_DIR / "failed_images.json"
FAILED_DETAILS_FILE = DATA_DIR / "failed_details.json"
