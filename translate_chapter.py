import requests
import json
import os
from zipfile import ZipFile
from io import BytesIO

def download_chapter(chapter_id):
    url = f"https://api.mangadex.org/at-home/server/{chapter_id}?forcePort443=false"
    response = requests.get(url)
    response.raise_for_status()  # Raise an exception for bad status codes

    data = response.json()
    if data["result"] != "ok":
        print(f"Error: MangaDex API returned an error: {data}")
        return

    base_url = data["baseUrl"]
    chapter_hash = data["chapter"]["hash"]
    image_data = data["chapter"]["data"]

    chapter_dir = f"{chapter_id}"
    os.makedirs(chapter_dir, exist_ok=True)

    for image_name in image_data:
        image_url = f"{base_url}/data/{chapter_hash}/{image_name}"
        image_path = os.path.join(chapter_dir, image_name)
        try:
            image_response = requests.get(image_url, stream=True)
            image_response.raise_for_status()

            with open(image_path, 'wb') as f:
                for chunk in image_response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"Downloaded: {image_name}")
        except requests.exceptions.RequestException as e:
            print(f"Error downloading {image_name}: {e}")

def translate_chapter(chapter_id, config_path=None):
    """
    Download a chapter, then send all images in the chapter folder to the batch translation endpoint.
    """
    from pathlib import Path
    import glob
    import time

    # Download chapter images
    download_chapter(chapter_id)

    # Prepare images for batch upload
    chapter_dir = Path(chapter_id)
    image_files = sorted(glob.glob(str(chapter_dir / '*.jpg')) + glob.glob(str(chapter_dir / '*.png')))
    if not image_files:
        print("No images found in chapter folder.")
        return

    # Create batch
    batch_req = {"name": f"Chapter {chapter_id}", "description": f"Auto batch for {chapter_id}"}
    resp = requests.post("http://localhost:8000/batch/create", json=batch_req)
    resp.raise_for_status()
    batch_id = resp.json()["batch_id"]
    print(f"Batch created: {batch_id}")

    # Prepare config
    config = "{}"
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            config = f.read()

    # Upload images to batch
    files = [("images", open(img, "rb")) for img in image_files]
    data = {"config": config}
    resp = requests.post(f"http://localhost:8000/batch/{batch_id}/add-multiple", files=files, data=data)
    resp.raise_for_status()
    print(f"Uploaded {len(image_files)} images to batch {batch_id}")
    print("You can check progress at /history-ui or /batch-ui.")

# Example usage:
if __name__ == "__main__":
    chapter_id = "b94d855e-632b-49c1-a776-0b31cc944dbb"
    translate_chapter(chapter_id)
