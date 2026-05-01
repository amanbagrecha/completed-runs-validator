import re
from dataclasses import dataclass
from datetime import datetime

import boto3

from app.config import AWS_PROFILE, AWS_REGION, BATCH_PREFIXES, S3_BUCKET


TAR_NAME_RE = re.compile(r"([^/]+)\.tar(?:\.gz)?$", re.IGNORECASE)


@dataclass(frozen=True)
class S3RunObject:
    run_id: str
    batch_name: str
    prefix: str
    key: str
    size: int | None
    last_modified: datetime | None


def get_s3_client():
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return session.client("s3", region_name=AWS_REGION)


def list_run_tars() -> dict[str, S3RunObject]:
    client = get_s3_client()
    runs: dict[str, S3RunObject] = {}
    for batch_name, prefix in BATCH_PREFIXES:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                match = TAR_NAME_RE.search(key)
                if not match:
                    continue
                run_id = match.group(1)
                runs[run_id] = S3RunObject(
                    run_id=run_id,
                    batch_name=batch_name,
                    prefix=prefix,
                    key=key,
                    size=obj.get("Size"),
                    last_modified=obj.get("LastModified"),
                )
    return runs
