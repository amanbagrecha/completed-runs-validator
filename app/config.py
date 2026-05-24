from dataclasses import dataclass
import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
CACHE_ROOT_DIR = DATA_DIR / "cache"
CACHE_DIR = CACHE_ROOT_DIR / "images"
AWS_DB_PATH = DATA_DIR / "aws_app.db"
AWS_CACHE_DIR = CACHE_ROOT_DIR / "aws-images"

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mfeHbvlI0CrT54WUC_stFTuXqg4a9a7YeVqZ5wHFbo8/"
    "gviz/tq?tqx=out:csv&gid=514978031"
)
AUTH_FILE_PATH = DATA_DIR / "auth.txt"


@dataclass(frozen=True)
class DatasetConfig:
    slug: str
    label: str
    page_path: str
    api_prefix: str
    db_path: Path
    cache_dir: Path
    aws_profile: str
    aws_region: str
    s3_bucket: str
    batch_prefixes: list[tuple[str, str]]


@dataclass(frozen=True)
class AuthUser:
    username: str
    password: str


@dataclass(frozen=True)
class AuthConfig:
    secret: str
    users: tuple[AuthUser, ...]
    cookie_name: str = "compltd_auth"
    session_max_age_seconds: int = 60 * 60 * 12


WASABI_BATCH_RANGE_START = 11
WASABI_BATCH_RANGE_END = 56
WASABI_BATCH_PREFIXES = [
    (f"batch-{batch_number:02d}", f"batch-{batch_number:02d}/")
    for batch_number in range(WASABI_BATCH_RANGE_START, WASABI_BATCH_RANGE_END + 1)
]

AWS_BATCH_PREFIXES = [
    ("panoramic_clean", "panoramic_clean/"),
    ("batch2", "batch2/"),
    *[(f"batch-{batch_number:02d}", f"batch-{batch_number:02d}/") for batch_number in range(3, 11)],
]

WASABI_DATASET = DatasetConfig(
    slug="wasabi",
    label="Wasabi Runs",
    page_path="/runreview",
    api_prefix="/api",
    db_path=DB_PATH,
    cache_dir=CACHE_DIR,
    aws_profile="wasabi",
    aws_region="us-west-1",
    s3_bucket="pano-processed-runs",
    batch_prefixes=WASABI_BATCH_PREFIXES,
)

AWS_DATASET = DatasetConfig(
    slug="aws",
    label="AWS Legacy Runs",
    page_path="/aws",
    api_prefix="/aws/api",
    db_path=AWS_DB_PATH,
    cache_dir=AWS_CACHE_DIR,
    aws_profile="s3",
    aws_region="us-east-1",
    s3_bucket="aipanoexport-batch2",
    batch_prefixes=AWS_BATCH_PREFIXES,
)

DATASETS = (WASABI_DATASET, AWS_DATASET)

DEFAULT_IMAGE_COUNT = 6
JPEG_QUALITY = 75


def _load_auth_file_values() -> dict[str, str]:
    if not AUTH_FILE_PATH.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in AUTH_FILE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


_AUTH_FILE_VALUES = _load_auth_file_values()


def _auth_value(name: str) -> str:
    return os.getenv(name, _AUTH_FILE_VALUES.get(name, ""))


def _load_auth_users() -> tuple[AuthUser, ...]:
    users: list[AuthUser] = []
    env_pairs = [
        (_auth_value("COMPLTD_ADMIN_USERNAME"), _auth_value("COMPLTD_ADMIN_PASSWORD")),
        (_auth_value("COMPLTD_USER_USERNAME"), _auth_value("COMPLTD_USER_PASSWORD")),
    ]
    seen: set[str] = set()
    for username, password in env_pairs:
        if not username and not password:
            continue
        if not username or not password:
            continue
        if username in seen:
            continue
        seen.add(username)
        users.append(AuthUser(username=username, password=password))
    return tuple(users)


AUTH_CONFIG = AuthConfig(
    secret=_auth_value("COMPLTD_AUTH_SECRET"),
    users=_load_auth_users(),
)
