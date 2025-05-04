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
R2_BUCKET = os.getenv('R2_BUCKET')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY')
R2_ENDPOINT = os.getenv('R2_ENDPOINT')
R2_URLPUBLIC = os.getenv('R2_URLPUBLIC')

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
    USE_R2 = check_r2_credentials()
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
        
        # Clean up any temporary files associated with this task
        cleanup_temp_files(task_id)
        
        logger.info(f"Image for task {task_id} uploaded to R2: {key}")
        return f"{R2_URLPUBLIC}/{key}"
    except Exception as e:
        logger.error(f"[R2] Upload failed: {e}")
        raise

def cleanup_temp_files(task_id: str):
    """Clean up any temporary files associated with this task"""
    try:
        upload_cache_dir = "upload-cache"
        if os.path.exists(upload_cache_dir):
            for filename in os.listdir(upload_cache_dir):
                if task_id in filename:
                    file_path = os.path.join(upload_cache_dir, filename)
                    if os.path.isfile(file_path):
                        try:
                            os.remove(file_path)
                            logger.info(f"Cleaned up temporary file after R2 upload: {file_path}")
                        except Exception as e:
                            logger.error(f"Error removing file {file_path}: {str(e)}")
    except Exception as e:
        logger.error(f"Error cleaning up temporary files for task {task_id}: {str(e)}")

def save_result_image_sync(image, task_id: str) -> str:
    """
    Synchronous version of save_result_image for use in non-async contexts.
    """
    import asyncio
    try:
        # Create a new event loop if needed
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If we're in a running event loop (which might happen in some contexts),
            # we must use a different approach
            return _save_result_image_sync_impl(image, task_id)
        else:
            # If no loop is running, we can create one and run the async function
            return loop.run_until_complete(save_result_image(image, task_id))
    except RuntimeError:
        # No event loop in this thread
        return _save_result_image_sync_impl(image, task_id)

def _save_result_image_sync_impl(image, task_id: str) -> str:
    """
    Internal implementation of synchronous image saving to R2.
    """
    today = datetime.datetime.now().strftime("%Y%m%d")
    filename = f"{task_id}.png"
    USE_R2 = check_r2_credentials()
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
        
        # Clean up any temporary files
        cleanup_temp_files(task_id)
        
        logger.info(f"Image for task {task_id} uploaded to R2: {key}")
        return f"{R2_URLPUBLIC}/{key}"
    except Exception as e:
        logger.error(f"[R2] Upload failed: {e}")
        raise

def get_result_path(path_or_url: str) -> str:
    """
    Convert a result path to a public URL.
    - If it's already a full URL, return it as is
    - If it's a relative path, convert it to a public URL
    """
    if not path_or_url:
        return ""
        
    # If it's already a full URL, return it
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
        
    # If it's an R2 path like results/20230101/task_id.png
    if path_or_url.startswith("results/"):
        return f"{R2_URLPUBLIC}/{path_or_url}"
        
    # For local file paths, convert to a local URL
    if os.path.exists(path_or_url):
        # Handle local file paths by converting to a relative URL path
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        rel_path = os.path.relpath(path_or_url, base_dir)
        
        # Convert Windows path separators to URL format
        rel_path = rel_path.replace('\\', '/')
        
        # Check if it's in results directory
        if "results" in rel_path:
            return f"/static/{os.path.join(*rel_path.split('/')[1:])}" 
        else:
            return f"/static/{rel_path}"
            
    # Default: return as is
    return path_or_url
