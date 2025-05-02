# prompt: https://api.mangadex.org/at-home/server/44332b36-8181-4537-a8f1-cb6085b06b1e?forcePort443=false
# dan response nya
# ```
# {"result":"ok","baseUrl":"https:\/\/cmdxd98sb0x3yprd.mangadex.network","chapter":{"hash":"02eb1faba1e50bfe6fe9c6d85e7f74a6","data":["1-ebad69dfed69a76ad385a7253b3637de9db3392ec157a65e5dbbf963c03f7938.jpg","2-e49c7f3ad7fdeedd2432be9f9cc45544d3880130a2c522543dfd674d31ab40e9.jpg","3-5e84545ae1266c95272b4ba90ef88be35ea82337388dca61e0697d7a1556ea01.jpg","4-397c41183ad046177b1b6c430015b2bd40edc725959534eade68fad09eb010bf.jpg","5-92579cebf37ebd58938b621d5c320f230cba1022273498ff66e42eb12e60c302.jpg","6-b890791c2af5a8c4c82bc36667f34648ec0b458da57d0f1243083cfa3a583d4f.jpg","7-555cd0c5ba8059b2c1d5cefbf47219a478877acf056ef573323f73256486a27d.jpg","8-ae430ef64c20bef0b27440e116d13e2d1dda153b2d2fd780bbfb6e3b07230b51.jpg","9-7a89f7974e6dd4de8455ce07dbc0511932bcb4bbbc063e054183ddd7c246cfb0.jpg","10-a62cb8b303564987ffef0527e1c152cd4412f62ad22a9854ad24c1f1602c142a.jpg","11-13dce718fb88ab7992bb8f567f6a933a7d935ddc3ed8c47dd158b260786a6ac5.jpg",....],
# ```
# lalu auto download sesuai urutan dengan example
# https://cmdxd98sb0x3yprd.mangadex.network/data/02eb1faba1e50bfe6fe9c6d85e7f74a6/2-e49c7f3ad7fdeedd2432be9f9cc45544d3880130a2c522543dfd674d31ab40e9.jpg
# dan simpan sebuah folder menyesuaikan chapter id dan tombol download zip nya

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


# Example usage:
chapter_id = "b94d855e-632b-49c1-a776-0b31cc944dbb"
download_chapter(chapter_id)
