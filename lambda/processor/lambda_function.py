import io
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3
from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
sns = boto3.client("sns")
dynamodb = boto3.client("dynamodb")

DEST_BUCKET = "image-pipeline-moiz-dest-0001"
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
TABLE_NAME = os.environ.get("TABLE_NAME", "image-pipeline-metadata")
MAX_WIDTH = 800
WATERMARK_TEXT = "Moiz Image Pipeline"
ALLOWED_EXT = (".jpg", ".jpeg", ".png")


def notify(subject, message):
    """Send an SNS message. Never raises, so a notification problem
    cannot break image processing or SQS retries."""
    if not SNS_TOPIC_ARN:
        logger.warning("SNS_TOPIC_ARN not set, skipping notification")
        return
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],
            Message=message,
        )
    except Exception:
        logger.exception("Failed to publish SNS notification")


def save_metadata(source_bucket, key, dest_key, info):
    """Write one metadata record. Same key -> overwritten on retry (idempotent).
    Never raises, so a DynamoDB problem cannot break image processing."""
    try:
        dynamodb.put_item(
            TableName=TABLE_NAME,
            Item={
                "source_key": {"S": key},
                "source_bucket": {"S": source_bucket},
                "dest_bucket": {"S": DEST_BUCKET},
                "dest_key": {"S": dest_key},
                "original_width": {"N": str(info["orig_w"])},
                "original_height": {"N": str(info["orig_h"])},
                "original_bytes": {"N": str(info["orig_bytes"])},
                "processed_width": {"N": str(info["new_w"])},
                "processed_height": {"N": str(info["new_h"])},
                "processed_bytes": {"N": str(info["new_bytes"])},
                "status": {"S": "PROCESSED"},
                "processed_at": {"S": datetime.now(timezone.utc).isoformat()},
            },
        )
    except Exception:
        logger.exception("Failed to save metadata to DynamoDB")


def get_font(size):
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def add_watermark(img):
    draw = ImageDraw.Draw(img)
    font = get_font(max(14, img.width // 25))
    left, top, right, bottom = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
    text_w, text_h = right - left, bottom - top
    x = max(5, img.width - text_w - 15)
    y = max(5, img.height - text_h - 15)
    draw.text((x + 1, y + 1), WATERMARK_TEXT, font=font, fill=(0, 0, 0, 180))
    draw.text((x, y), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 230))
    return img


def process_image(bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    data = obj["Body"].read()
    orig_bytes = len(data)

    img = Image.open(io.BytesIO(data))
    fmt = (img.format or "").upper()
    img = ImageOps.exif_transpose(img)
    orig_w, orig_h = img.width, img.height

    if img.width > MAX_WIDTH:
        new_h = int(img.height * (MAX_WIDTH / img.width))
        img = img.resize((MAX_WIDTH, new_h), Image.LANCZOS)

    if fmt == "JPEG":
        img = img.convert("RGB")
        out_format, content_type = "JPEG", "image/jpeg"
    else:
        img = img.convert("RGBA")
        out_format, content_type = "PNG", "image/png"

    img = add_watermark(img)

    buf = io.BytesIO()
    if out_format == "JPEG":
        img.save(buf, format="JPEG", quality=85)
    else:
        img.save(buf, format="PNG")
    buf.seek(0)
    out_bytes = buf.getvalue()

    dest_key = f"processed/{key}"  # same input -> same output key (safe for retries)
    s3.put_object(
        Bucket=DEST_BUCKET,
        Key=dest_key,
        Body=out_bytes,
        ContentType=content_type,
    )
    logger.info("Processed s3://%s/%s -> s3://%s/%s", bucket, key, DEST_BUCKET, dest_key)

    info = {
        "orig_w": orig_w,
        "orig_h": orig_h,
        "orig_bytes": orig_bytes,
        "new_w": img.width,
        "new_h": img.height,
        "new_bytes": len(out_bytes),
    }
    return dest_key, info


def lambda_handler(event, context):
    for sqs_record in event.get("Records", []):
        body = json.loads(sqs_record["body"])

        # S3 sends a test message when notification is created
        if body.get("Event") == "s3:TestEvent":
            logger.info("Skipping s3:TestEvent message")
            continue

        for s3_record in body.get("Records", []):
            bucket = s3_record["s3"]["bucket"]["name"]
            key = unquote_plus(s3_record["s3"]["object"]["key"])

            if not key.lower().endswith(ALLOWED_EXT):
                logger.info("Skipping non-image file: %s", key)
                continue

            try:
                dest_key, info = process_image(bucket, key)
            except Exception as e:
                notify(
                    "Image pipeline FAILURE",
                    f"Processing failed.\nSource: s3://{bucket}/{key}\n"
                    f"Error: {type(e).__name__}: {e}\n"
                    f"SQS will retry; after 3 failed receives the message goes to the DLQ.",
                )
                # Re-raise so SQS retries, then DLQ after 3 receives
                raise

            save_metadata(bucket, key, dest_key, info)
            notify(
                "Image pipeline SUCCESS",
                f"Image processed.\nSource: s3://{bucket}/{key}\n"
                f"Output: s3://{DEST_BUCKET}/{dest_key}",
            )

    return {"status": "done"}