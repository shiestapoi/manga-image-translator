import os
import datetime
import zipfile
import logging
from typing import Optional
from io import BytesIO

try:
    import boto3
    from botocore.exceptions import BotoCoreError, NoCredentialsError
except ImportError:
    boto3 = None

# Configure logging
logger = logging.getLogger("storage")

# Read R2 configuration from environment variables
R2_BUCKET = os.getenv('R2_BUCKET', "cotrans-public")
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID', "d8f9a2dddabc760c60234279b5ab1f42")
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY', "a4325c27d9027dcc89dd26723496f94a5304c89814e7c2fcb135d56d70bd828c")
R2_ENDPOINT = os.getenv('R2_ENDPOINT', "https://4c552ebd3ca0cc67336078e3eba7600d.r2.cloudflarestorage.com")
R2_URLPUBLIC = os.getenv('R2_URLPUBLIC', "https://r2.cotrans.dawg.web.id")

def check_r2_credentials():
    """Check if R2 credentials are properly configured"""
    if not boto3:
        logger.warning("boto3 is not installed, R2 storage will not be available")
        return False
    
    if not all([R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT]):
        logger.warning("R2 credentials are not properly configured")
        return False
    
    try:
        session = boto3.session.Session()
        s3 = session.client(
            service_name="s3",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            endpoint_url=R2_ENDPOINT,
        )
        
        # Try a basic operation
        s3.head_bucket(Bucket=R2_BUCKET)
        logger.info("R2 credentials verified successfully")
        return True
    except Exception as e:
        logger.error(f"Error checking R2 credentials: {e}")
        return False

async def save_result_image(image, task_id: str) -> str:
    """
    Save result image to R2. Raises if R2 is not available.
    Returns the URL to the saved image.
    """
    today = datetime.datetime.now().strftime("%Y%m%d")
    filename = f"{task_id}.png"
    USE_R2 = check_r2_credentials()  # Remove await here
    if not USE_R2:
        logger.error("R2 storage is not configured, aborting save.")
        raise RuntimeError("R2 storage is not available")
    
    # buffer image
    import io
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    key = f"results/{today}/{filename}"
    try:
        session = boto3.session.Session()
        s3 = session.client(
            service_name="s3",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            endpoint_url=R2_ENDPOINT,
        )
        s3.upload_fileobj(
            buf, 
            R2_BUCKET, 
            key, 
            ExtraArgs={"ContentType": "image/png", "ACL": "public-read"}
        )
        # optional: upload zip if still needed
        logger.info(f"Image for task {task_id} uploaded to R2: {key}")
        return f"{R2_URLPUBLIC}/{key}"
    except Exception as e:
        logger.error(f"[R2] Upload failed: {e}")
        raise
