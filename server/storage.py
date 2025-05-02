import os
import datetime
import zipfile
from typing import Optional
from io import BytesIO

try:
    import boto3
    from botocore.exceptions import BotoCoreError, NoCredentialsError
except ImportError:
    boto3 = None

R2_BUCKET = "cotrans-public"
R2_ACCESS_KEY_ID = "d8f9a2dddabc760c60234279b5ab1f42"
R2_SECRET_ACCESS_KEY = "a4325c27d9027dcc89dd26723496f94a5304c89814e7c2fcb135d56d70bd828c"
R2_ENDPOINT = "https://4c552ebd3ca0cc67336078e3eba7600d.r2.cloudflarestorage.com"

USE_R2 = all([R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT, boto3])

def save_result_image(image, task_id: str) -> str:
    """
    Save result image to local or R2. Copy to static for local, return static-accessible path or R2 URL.
    """
    today = datetime.datetime.now().strftime("%Y%m%d")
    filename = f"{task_id}.png"
    
    # Try to use R2 if configured
    print(USE_R2)
    if USE_R2:
        # Save to R2
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
            
            # Upload the PNG image
            s3.upload_fileobj(buf, R2_BUCKET, key, ExtraArgs={"ContentType": "image/png", "ACL": "public-read"})
            
            # Create and upload a ZIP version
            zip_buf = BytesIO()
            with zipfile.ZipFile(zip_buf, 'w', compression=zipfile.ZIP_DEFLATED) as zip_file:
                zip_file.writestr(filename, buf.getvalue())
            
            zip_buf.seek(0)
            zip_key = f"results/{today}/{task_id}.zip"
            s3.upload_fileobj(zip_buf, R2_BUCKET, zip_key, ExtraArgs={"ContentType": "application/zip", "ACL": "public-read"})
            
            # Return the URL to the original PNG
            url = f"{R2_ENDPOINT}/{R2_BUCKET}/{key}"
            return url
        except Exception as e:
            print(f"[R2] Upload failed, fallback to local: {e}")
    
    # Save locally when USE_R2 is False or R2 upload failed
    result_dir = os.path.join("history", "results", today)
    os.makedirs(result_dir, exist_ok=True)
    result_path = os.path.join(result_dir, filename)
    image.save(result_path)
    
    # Create a copy in static directory for web access
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "results")
    os.makedirs(os.path.join(static_dir, today), exist_ok=True)
    
    static_path = os.path.join(static_dir, today, filename)
    
    try:
        import shutil
        if not os.path.exists(static_path):
            shutil.copy2(result_path, static_path)
    except Exception as e:
        print(f"[Static Copy] Failed: {e}")
    
    # Return static path for frontend
    return f"{today}/{filename}"
