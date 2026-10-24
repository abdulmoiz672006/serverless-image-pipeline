import json
import os
import re
import uuid

import boto3
from botocore.config import Config

BUCKET = "image-pipeline-moiz-source-0001"
REGION = "us-east-1"
EXPIRES_SECONDS = 300
ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png")

s3 = boto3.client(
    "s3",
    region_name=REGION,
    config=Config(signature_version="s3v4"),
)


def make_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    params = (event or {}).get("queryStringParameters") or {}
    raw_name = params.get("filename", "")

    # Keep only the file name part, remove any folder tricks
    name = os.path.basename(raw_name.replace("\\", "/")).strip().lower()
    stem, ext = os.path.splitext(name)

    if not name or ext not in ALLOWED_EXTENSIONS:
        return make_response(
            400, {"error": "filename is required and must end with .jpg, .jpeg or .png"}
        )

    # Replace unsafe characters and limit the length
    safe_stem = re.sub(r"[^a-z0-9_-]", "_", stem)[:50] or "image"
    key = f"uploads/{uuid.uuid4().hex[:8]}-{safe_stem}{ext}"

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=EXPIRES_SECONDS,
        HttpMethod="PUT",
    )

    return make_response(
        200,
        {
            "upload_url": upload_url,
            "key": key,
            "expires_in_seconds": EXPIRES_SECONDS,
            "method": "PUT",
        },
    )