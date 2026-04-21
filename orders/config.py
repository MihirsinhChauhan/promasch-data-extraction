import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://gw.promasch.in"
LOGIN_USER = os.getenv(
    "ORDER_USER",
    os.getenv("PROMASCH_USER", "Vikram@greenwave.ws"),
)
LOGIN_PASSWORD = os.getenv(
    "ORDER_PASSWORD",
    os.getenv("PROMASCH_PASSWORD", "Infosys@9009"),
)

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_BUCKET_NAME = os.getenv("AWS_BUCKET_NAME", "promasch-orders")

# PDF endpoint — same auth posture as BillPdf (typically public)
ORDER_PDF_URL = f"{BASE_URL}/OrderPdf"
ORDER_TYPE = "PO1"   # orderType query param for PO PDFs

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PO_CATALOG_FILE = DATA_DIR / "po_catalog.json"
FAILED_IDS_FILE = DATA_DIR / "failed_ids.json"
FINAL_OUTPUT_FILE = DATA_DIR / "final_output.json"

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.1"))
PDF_TIMEOUT = int(os.getenv("PDF_TIMEOUT", "30"))
PDF_RETRIES = int(os.getenv("PDF_RETRIES", "3"))

# Scroll tuning — more conservative than vendor-bills to handle virtual scroll
SCROLL_PAUSE_MS = 1500
SCROLL_STABLE_THRESHOLD = 10   # rounds with no new POs before stopping
