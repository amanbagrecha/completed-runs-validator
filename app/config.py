from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache" / "images"
DB_PATH = DATA_DIR / "app.db"

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mfeHbvlI0CrT54WUC_stFTuXqg4a9a7YeVqZ5wHFbo8/"
    "export?format=csv&gid=2085465220"
)

AWS_PROFILE = "wasabi"
AWS_REGION = "us-west-1"
S3_BUCKET = "pano-processed-runs"

BATCH_RANGE_START = 11
BATCH_RANGE_END = 50

BATCH_PREFIXES = [
    (f"batch-{batch_number:02d}", f"batch-{batch_number:02d}/")
    for batch_number in range(BATCH_RANGE_START, BATCH_RANGE_END + 1)
]

DEFAULT_IMAGE_COUNT = 3
JPEG_QUALITY = 75
