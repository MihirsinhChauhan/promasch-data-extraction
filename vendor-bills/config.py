import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://gw.promasch.in"
LOGIN_USER = os.getenv("PROMASCH_USER", "Vikram@greenwave.ws")
LOGIN_PASSWORD = os.getenv("PROMASCH_PASSWORD", "Infosys@9009")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_BUCKET_NAME = os.getenv("AWS_BUCKET_NAME", "vendors-bills")

PDF_BASE_URL = f"{BASE_URL}/BillPdf"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PO_METADATA_FILE = DATA_DIR / "po_metadata.json"
WO_METADATA_FILE = DATA_DIR / "wo_metadata.json"
FAILED_IDS_FILE = DATA_DIR / "failed_ids.json"
FINAL_OUTPUT_FILE = DATA_DIR / "final_output.json"

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.1"))
PDF_TIMEOUT = int(os.getenv("PDF_TIMEOUT", "30"))
PDF_RETRIES = int(os.getenv("PDF_RETRIES", "3"))
SCROLL_PAUSE_MS = 1200
SCROLL_STABLE_THRESHOLD = 8
