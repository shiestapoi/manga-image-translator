import io
import os
import secrets
import shutil
import signal
import subprocess
import sys
import logging
import uuid
from argparse import Namespace
from typing import List, Dict, Any, Optional
from pathlib import Path
import datetime
import asyncio
import pickle

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fastapi import FastAPI, Request, HTTPException, Header, UploadFile, File, Form, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from manga_translator import Config
from server.instance import ExecutorInstance, executor_instances
from server.myqueue import task_queue, QueueElement, BatchJob
from server.request_extraction import get_ctx, while_streaming, TranslateRequest, to_pil_image, wait_in_queue
from server.to_json import to_translation, TranslationResponse
from pydantic import BaseModel
from server.storage import save_result_image, save_result_image_sync, get_result_path
from server.streaming import notify, stream
# Import database functions
from server.database import save_task, update_task, get_task, get_recent_tasks
from server.database import create_batch as db_create_batch
from server.database import get_batch as db_get_batch
from server.database import get_batch_tasks as db_get_batch_tasks
from server.database import list_batches as db_list_batches

import httpx

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("manga_translator_server")

app = FastAPI(
    title="Manga Image Translator API",
    description="API for translating manga and other images with text",
    version="1.0.0"
)
nonce = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        logger.info(f"Request path: {request.url.path}")
        response = await call_next(request)
        return response

app.add_middleware(LoggingMiddleware)

# Create folders if they don't exist
os.makedirs("upload-cache", exist_ok=True)
os.makedirs(os.path.join("history", "results"), exist_ok=True)

@app.on_event("startup")
async def startup_event():
    """Start background tasks when the application starts."""
    await task_queue.start_background_tasks()
    await executor_instances.start_background_tasks()
    logger.info("Background tasks started.")
    
    # Aggressive cleanup of the upload-cache directory on startup
    try:
        upload_cache_dir = "upload-cache"
        if os.path.exists(upload_cache_dir):
            deleted_files = 0
            for item in os.listdir(upload_cache_dir):
                item_path = os.path.join(upload_cache_dir, item)
                # Keep directories as they might be in use (like mangadex-*)
                if os.path.isfile(item_path):
                    try:
                        os.remove(item_path)
                        deleted_files += 1
                    except Exception as e:
                        logger.error(f"Error removing file {item_path}: {str(e)}")
                
            logger.info(f"Startup cleanup: removed {deleted_files} files from upload-cache")
    except Exception as e:
        logger.error(f"Error cleaning up upload cache on startup: {str(e)}")
    
    # Initialize database tables if needed
    from server.database import init_database
    if init_database():
        logger.info("Database tables initialized successfully")
    else:
        logger.warning("Failed to initialize database tables")

# New models for batch processing
class BatchRequest(BaseModel):
    name: str
    description: Optional[str] = None
    
class TaskStatus(BaseModel):
    task_id: str
    status: str
    queue_position: Optional[int] = None
    error_message: Optional[str] = None
    
class BatchStatus(BaseModel):
    batch_id: str
    name: str
    description: Optional[str] = None
    status: str
    progress: float
    total_tasks: int
    completed_tasks: int
    failed_tasks: int

    # Serve static files
    app.mount("/static", StaticFiles(directory="server/static"), name="static")

@app.post("/register", response_description="no response", tags=["internal-api"])
async def register_instance(instance: ExecutorInstance, req: Request, req_nonce: str = Header(alias="X-Nonce")):
    if req_nonce != nonce:
        raise HTTPException(401, detail="Invalid nonce")
    # Allow external connections by using the client's real IP
    client_host = req.client.host
    # If the client is coming through a proxy, use the X-Forwarded-For header
    forwarded_for = req.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_host = forwarded_for.split(",")[0].strip()
    
    # For SSH tunneling, we might need to use a fixed IP
    if os.environ.get("USE_FIXED_IP"):
        client_host = os.environ.get("FIXED_IP", "127.0.0.1")
    
    instance.ip = client_host
    logger.info(f"Registering translator instance at {instance.ip}:{instance.port}")
    executor_instances.register(instance)

def transform_to_image(ctx):
    img_byte_arr = io.BytesIO()
    ctx.result.save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()

def transform_to_json(ctx):
    return to_translation(ctx).model_dump_json().encode("utf-8")

def transform_to_bytes(ctx):
    return to_translation(ctx).to_bytes()

@app.post("/translate/json", response_model=TranslationResponse, tags=["api", "json"], response_description="json structure inspired by the ichigo translator extension")
async def json(req: Request, data: TranslateRequest):
    ctx = await get_ctx(req, data.config, data.image)
    return to_translation(ctx)

@app.post("/translate/bytes", response_class=StreamingResponse, tags=["api", "json"],response_description="custom byte structure for decoding look at examples in 'examples/response.*'")
async def bytes(req: Request, data: TranslateRequest):
    ctx = await get_ctx(req, data.config, data.image)
    return StreamingResponse(content=to_translation(ctx).to_bytes())

@app.post("/translate/image", response_description="the result image", tags=["api", "json"],response_class=StreamingResponse)
async def image(req: Request, data: TranslateRequest) -> StreamingResponse:
    ctx = await get_ctx(req, data.config, data.image)
    img_byte_arr = io.BytesIO()
    ctx.result.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)

    return StreamingResponse(img_byte_arr, media_type="image/png")

@app.post("/translate/json/stream", response_class=StreamingResponse,tags=["api", "json"], response_description="A stream over elements with strucure(1byte status, 4 byte size, n byte data) status code are 0,1,2,3,4 0 is result data, 1 is progress report, 2 is error, 3 is waiting queue position, 4 is waiting for translator instance")
async def stream_json(req: Request, data: TranslateRequest) -> StreamingResponse:
    return await while_streaming(req, transform_to_json, data.config, data.image)

@app.post("/translate/bytes/stream", response_class=StreamingResponse, tags=["api", "json"],response_description="A stream over elements with strucure(1byte status, 4 byte size, n byte data) status code are 0,1,2,3,4 0 is result data, 1 is progress report, 2 is error, 3 is waiting queue position, 4 is waiting for translator instance")
async def stream_bytes(req: Request, data: TranslateRequest)-> StreamingResponse:
    return await while_streaming(req, transform_to_bytes,data.config, data.image)

@app.post("/translate/image/stream", response_class=StreamingResponse, tags=["api", "json"], response_description="A stream over elements with strucure(1byte status, 4 byte size, n byte data) status code are 0,1,2,3,4 0 is result data, 1 is progress report, 2 is error, 3 is waiting queue position, 4 is waiting for translator instance")
async def stream_image(req: Request, data: TranslateRequest) -> StreamingResponse:
    return await while_streaming(req, transform_to_image, data.config, data.image)

@app.post("/translate/with-form/json", response_model=TranslationResponse, tags=["api", "form"],response_description="json strucure inspired by the ichigo translator extension")
async def json_form(req: Request, image: UploadFile = File(...), config: str = Form("{}")):
    img = await image.read()
    ctx = await get_ctx(req, Config.parse_raw(config), img)
    return to_translation(ctx)

@app.post("/translate/with-form/bytes", response_class=StreamingResponse, tags=["api", "form"],response_description="custom byte structure for decoding look at examples in 'examples/response.*'")
async def bytes_form(req: Request, image: UploadFile = File(...), config: str = Form("{}")):
    img = await image.read()
    ctx = await get_ctx(req, Config.parse_raw(config), img)
    return StreamingResponse(content=to_translation(ctx).to_bytes())

@app.post("/translate/with-form/image", response_description="the result image", tags=["api", "form"],response_class=StreamingResponse)
async def image_form(
    req: Request,
    image: UploadFile = File(...),
    config: str = Form("{}")
) -> StreamingResponse:
    # generate and record task
    task_id = uuid.uuid4().hex
    cfg = Config.parse_raw(config)
    save_task(task_id, None, "processing", cfg.dict())

    # perform translation
    img_bytes = await image.read()
    ctx = await get_ctx(req, cfg, img_bytes)

    # persist result to R2 and update DB
    result_path = save_result_image(ctx.result, task_id)
    update_task(task_id, "completed", result_path=result_path)

    # return PNG stream with task id
    buf = io.BytesIO()
    ctx.result.save(buf, format="PNG")
    buf.seek(0)
    resp = StreamingResponse(buf, media_type="image/png")
    resp.headers["X-Task-ID"] = task_id
    return resp

@app.post("/translate/with-form/json/stream", response_class=StreamingResponse, tags=["api", "form"],response_description="A stream over elements with strucure(1byte status, 4 byte size, n byte data) status code are 0,1,2,3,4 0 is result data, 1 is progress report, 2 is error, 3 is waiting queue position, 4 is waiting for translator instance")
async def stream_json_form(req: Request, image: UploadFile = File(...), config: str = Form("{}")) -> StreamingResponse:
    img = await image.read()
    return await while_streaming(req, transform_to_json, Config.parse_raw(config), img)

@app.post("/translate/with-form/bytes/stream", response_class=StreamingResponse,tags=["api", "form"], response_description="A stream over elements with strucure(1byte status, 4 byte size, n byte data) status code are 0,1,2,3,4 0 is result data, 1 is progress report, 2 is error, 3 is waiting queue position, 4 is waiting for translator instance")
async def stream_bytes_form(req: Request, image: UploadFile = File(...), config: str = Form("{}"))-> StreamingResponse:
    img = await image.read()
    return await while_streaming(req, transform_to_bytes, Config.parse_raw(config), img)

@app.post("/translate/with-form/image/stream", response_class=StreamingResponse, tags=["api","form"],response_description="streaming PNG plus task persistence")
async def stream_image_form(
    req: Request,
    image: UploadFile = File(...),
    config: str = Form("{}")
) -> StreamingResponse:
    # generate and record task
    task_id = uuid.uuid4().hex
    cfg = Config.parse_raw(config)
    save_task(task_id, None, "processing", cfg.dict())

    # prepare image and queue element
    img_bytes = await image.read()
    pil_img = await to_pil_image(img_bytes)
    task = QueueElement(req, pil_img, cfg, 0)
    task_queue.add_task(task)

    # set up streaming queue
    messages = asyncio.Queue()

    def notify_internal(code: int, data: bytes) -> None:
        if code == 0:
            # final result: persist to R2 and DB
            ctx = pickle.loads(data)
            asyncio.create_task(save_result_image(ctx.result, task_id)).add_done_callback(
                lambda fut: update_task(task_id, "completed", result_path=fut.result())
            )
        # forward chunk to client
        notify(code, data, transform_to_image, messages)

    # stream out 
    streaming_response = StreamingResponse(stream(messages), media_type="application/octet-stream")
    asyncio.create_task(wait_in_queue(task, notify_internal))
    return streaming_response

# Enhanced queue processing for single images
async def process_single_image_task(task: QueueElement):
    """Process a single image task with database storage."""
    from server.instance import executor_instances
    try:
        instance = await executor_instances.find_executor()
        if not instance:
            raise RuntimeError("No available translator instances.")
        
        task.status = "processing"
        # Update database status
        update_task(task.task_id, "processing")
        
        result = await instance.sent(task.get_image(), task.config)
        # Save result to R2 storage asynchronously
        asyncio.create_task(save_result_image(result.result, task.task_id)).add_done_callback(
            lambda fut: update_task(task.task_id, "completed", result_path=fut.result())
        )
        task.status = "completed"
        
        # Clean up cache files
        task.cleanup_files()
        # Also clean up any potential leftover files in upload-cache
        cleanup_task_files(task.task_id)
    except Exception as e:
        error_message = f"Error processing task {task.task_id}: {str(e)}"
        task.status = "failed"
        task.error_message = error_message
        logger.error(error_message)
        
        # Update database with error message
        update_task(task.task_id, "failed", error_message=error_message)
        
        # Clean up cache files even on error
        task.cleanup_files()
        cleanup_task_files(task.task_id)
    finally:
        if instance:
            await executor_instances.free_executor(instance)

# Batch processing with database storage
async def process_batch_task(task: QueueElement):
    from server.instance import executor_instances
    instance = None  # Initialize instance as None to prevent UnboundLocalError
    try:
        instance = await executor_instances.find_executor()
        task.status = "processing"
        # Update database status
        update_task(task.task_id, "processing")
        
        result = await instance.sent(task.get_image(), task.config)
        # Save result to R2 storage
        asyncio.create_task(save_result_image(result.result, task.task_id)).add_done_callback(
            lambda fut: update_task(task.task_id, "completed", result_path=fut.result())
        )
        task.status = "completed"
        
        # Clean up cache files after successful processing
        task.cleanup_files()
        cleanup_task_files(task.task_id)
    except Exception as e:
        error_message = str(e)
        task.status = "failed"
        task.error_message = error_message
        
        # Update database with error message
        update_task(task.task_id, "failed", error_message=error_message)
        
        # Clean up cache files even on error
        task.cleanup_files()
        cleanup_task_files(task.task_id)
    finally:
        if instance:  # Only try to free the executor if it was assigned
            await executor_instances.free_executor(instance)

def cleanup_task_files(task_id):
    """Clean up any files in upload-cache associated with this task"""
    try:
        upload_cache_dir = "upload-cache"
        if os.path.exists(upload_cache_dir):
            for filename in os.listdir(upload_cache_dir):
                if task_id in filename:
                    file_path = os.path.join(upload_cache_dir, filename)
                    if os.path.isfile(file_path):
                        try:
                            os.remove(file_path)
                            logger.info(f"Removed cache file for task {task_id}: {file_path}")
                        except Exception as e:
                            logger.error(f"Error removing cache file {file_path}: {str(e)}")
    except Exception as e:
        logger.error(f"Error in cleanup_task_files for task {task_id}: {str(e)}")

@app.post("/queue/single", response_model=TaskStatus, tags=["queue"])
async def queue_single_image(
    req: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    config: str = Form("{}")
):
    """Queue a single image for processing."""
    img_data = await image.read()
    img = await to_pil_image(img_data)
    config_obj = Config.parse_raw(config)
    task = QueueElement(req, img, config_obj, priority=2)
    
    # Save task to database
    save_task(task.task_id, None, "queued", config_obj.dict())
    
    task_queue.add_task(task)
    background_tasks.add_task(process_single_image_task, task)
    return TaskStatus(
        task_id=task.task_id,
        status=task.status,
        queue_position=task_queue.get_pos(task)
    )

@app.get("/queue/single/{task_id}", response_model=Dict[str, Any], tags=["queue"])
async def get_single_task_status(task_id: str):
    """Get the status of a single image task."""
    # Try database first
    task_data = get_task(task_id)
    if not task_data:
        # Fall back to in-memory
        task = task_queue.get_task(task_id)
        if not task:
            raise HTTPException(404, detail=f"Task {task_id} not found")
        task_data = task.to_dict()
    
    response = {
        "task_id": task_data["task_id"],
        "status": task_data["status"],
        "created_at": task_data["created_at"],
        "queue_position": task_queue.get_pos(task_queue.get_task_by_id(task_id)) if task_data["status"] == "queued" else None,
        "retries": task_data.get("retries", 0),
    }
    
    if task_data.get("error_message"):
        response["error_message"] = task_data["error_message"]
    
    if task_data.get("result_path"):
        response["result_url"] = get_result_path(task_data["result_path"])
    
    return response

@app.post("/batch/create", response_model=BatchStatus, tags=["batch"])
async def create_batch(batch_request: BatchRequest):
    """Create a new batch job for processing multiple images"""
    batch = task_queue.create_batch(batch_request.name, batch_request.description)
    
    # Save to database
    db_create_batch(batch.batch_id, batch_request.name, batch_request.description)
    
    return BatchStatus(**batch.to_dict())

@app.post("/batch/{batch_id}/add", response_model=TaskStatus, tags=["batch"])
async def add_to_batch(
    batch_id: str, 
    req: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...), 
    config: str = Form("{}")
):
    """Add an image to a batch job"""
    batch = task_queue.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, detail=f"Batch {batch_id} not found")
    
    img_data = await image.read()
    img = await to_pil_image(img_data)
    config_obj = Config.parse_raw(config)
    task = QueueElement(req, img, config_obj, priority=1, batch_id=batch_id)
    
    # Save to database
    save_task(task.task_id, batch_id, "queued", config_obj.dict())
    
    task_queue.add_task(task)
    background_tasks.add_task(process_batch_task, task)
    return TaskStatus(
        task_id=task.task_id,
        status=task.status,
        queue_position=task_queue.get_pos(task)
    )

@app.post("/batch/{batch_id}/add-multiple", response_model=List[TaskStatus], tags=["batch"])
async def add_multiple_to_batch(
    batch_id: str, 
    req: Request,
    background_tasks: BackgroundTasks,
    images: List[UploadFile] = File(...), 
    config: str = Form("{}")
):
    """Add multiple images to a batch job"""
    batch = task_queue.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, detail=f"Batch {batch_id} not found")
    
    statuses = []
    config_obj = Config.parse_raw(config)
    
    for image in images:
        img_data = await image.read()
        img = await to_pil_image(img_data)
        task = QueueElement(req, img, config_obj, priority=1, batch_id=batch_id)
        
        # Save to database
        save_task(task.task_id, batch_id, "queued", config_obj.dict())
        
        task_queue.add_task(task)
        background_tasks.add_task(process_batch_task, task)
        statuses.append(TaskStatus(
            task_id=task.task_id,
            status=task.status,
            queue_position=task_queue.get_pos(task)
        ))
    
    return statuses

@app.get("/batch/{batch_id}", response_model=BatchStatus, tags=["batch"])
async def get_batch_status(batch_id: str):
    """Get the status of a batch job"""
    # Try database first
    batch = db_get_batch(batch_id)
    if not batch:
        # Fall back to in-memory if database fails
        batch = task_queue.get_batch(batch_id)
        if not batch:
            raise HTTPException(404, detail=f"Batch {batch_id} not found")
        batch = batch.to_dict()
    
    return BatchStatus(**batch)

@app.get("/batch/{batch_id}/tasks", response_model=List[Dict[str, Any]], tags=["batch"])
async def get_batch_tasks(batch_id: str):
    """Get all tasks in a batch job"""
    # Try database first
    tasks = db_get_batch_tasks(batch_id)
    if not tasks:
        # Fall back to in-memory if database fails
        tasks = task_queue.get_batch_tasks(batch_id)
        if not tasks:
            raise HTTPException(404, detail=f"Batch {batch_id} not found or has no tasks")
    
    return tasks

@app.get("/batch", response_model=List[BatchStatus], tags=["batch"])
async def list_batches(limit: int = 50):
    """List all batch jobs"""
    # Try database first
    batches = db_list_batches(limit)
    if not batches:
        # Fall back to in-memory if database fails
        batches = task_queue.list_batches(limit)
    
    return [BatchStatus(**batch) for batch in batches]

@app.get("/history", response_model=List[Dict[str, Any]], tags=["history"])
async def get_history(limit: int = 50):
    """Get recent translation history"""
    # Try database first
    tasks = get_recent_tasks(limit)
    if not tasks:
        # Fall back to in-memory if database fails
        tasks = task_queue.get_recent_tasks(limit)
    
    return tasks

@app.post("/queue-size", response_model=int, tags=["api", "json"])
async def queue_size() -> int:
    return len(task_queue.queue)

@app.get("/queue/status", response_model=Dict[str, Any], tags=["queue"])
async def queue_status():
    """Get the status of the queue and executor instances"""
    return {
        "queue_size": len(task_queue.queue),
        "free_executors": executor_instances.free_executors(),
        "total_executors": len(executor_instances.list),
        "connected_executors": sum(1 for inst in executor_instances.list if inst.consecutive_errors == 0)
    }

@app.get("/queue/resume", response_model=Dict[str, Any], tags=["queue"])
async def get_queued_tasks(background_tasks: BackgroundTasks):
    """Get all tasks in the queue and resume processing them"""
    # Get all tasks with status "queued" from the database
    from server.database import get_queued_tasks
    queued_tasks = []
    try:
        queued_tasks = get_queued_tasks(50)  # Limit to 50 tasks
    except Exception as e:
        logger.error(f"Error getting queued tasks: {e}")
        queued_tasks = []
    
    # Process each task in the background
    resumed_tasks = 0
    for task_data in queued_tasks:
        try:
            # Get task config
            config_obj = Config.parse_raw(task_data.get("config", "{}"))
            
            # Create a dummy request object
            req = Request({"type": "http"})
            
            # Create a new task
            batch_id = task_data.get("batch_id")
            task = QueueElement(req, None, config_obj, priority=1 if batch_id else 2, batch_id=batch_id)
            task.task_id = task_data["task_id"]
            task.status = "queued"
            
            # Update database status
            update_task(task.task_id, "queued", error_message="Resuming after server restart")
            
            # Add to queue and process
            task_queue.add_task(task)
            
            # Process based on whether it's a batch task or single task
            if batch_id:
                background_tasks.add_task(process_batch_task, task)
            else:
                background_tasks.add_task(process_single_image_task, task)
            
            resumed_tasks += 1
            
        except Exception as e:
            logger.error(f"Error resuming task {task_data['task_id']}: {e}")
    
    return {
        "queued_tasks": len(queued_tasks),
        "resumed_tasks": resumed_tasks,
        "message": f"Resumed {resumed_tasks} of {len(queued_tasks)} queued tasks"
    }

@app.get("/", response_class=HTMLResponse,tags=["ui"])
async def index() -> HTMLResponse:
    script_directory = Path(__file__).parent
    html_file = script_directory / "index.html"
    html_content = html_file.read_text(encoding="utf-8")
    return HTMLResponse(content=html_content)

@app.get("/read", response_class=HTMLResponse, tags=["ui"])
async def read():
    script_directory = Path(__file__).parent
    html_file = script_directory / "read.html"
    html_content = html_file.read_text(encoding="utf-8")
    return HTMLResponse(content=html_content)

@app.get("/manual", response_class=HTMLResponse, tags=["ui"])
async def manual():
    script_directory = Path(__file__).parent
    html_file = script_directory / "manual.html"
    html_content = html_file.read_text(encoding="utf-8")
    return HTMLResponse(content=html_content)

@app.post("/start-translator", tags=["server"])
async def start_translator():
    """Start a new translator client process."""
    try:
        port = int(os.getenv("PORT", 8000)) + 1
        # Get the current host from environment or use the same as the main server
        current_host = os.getenv("HOST", "127.0.0.1")
        
        # Check if the translator client is already running
        # Verify if the port is actually in use
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((current_host, port)) == 0:
                logger.info(f"Translator client already running on port {port}")
                return {"message": f"Translator client already running on port {port}"}
        
        # Start a new translator client process
        start_translator_client_proc(current_host, port, nonce, Namespace())
        logger.info(f"Translator client started on port {port}")
        return {"message": f"Translator client started on port {port}"}
    except Exception as e:
        logger.error(f"Failed to start translator client: {e}")
        return {"error": "Failed to start translator client"}

@app.get("/proxy/mangadex/chapter/{chapter_id}", tags=["proxy"])
async def proxy_mangadex_chapter(chapter_id: str):
    """Proxy for MangaDex API to avoid CORS issues"""
    url = f"https://api.mangadex.org/at-home/server/{chapter_id}?forcePort443=false"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            return response.json()
    except Exception as e:
        logger.error(f"Error proxying MangaDex API: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch from MangaDex: {str(e)}")

@app.get("/proxy/mangadex/image/{chapter_hash}/{filename}", tags=["proxy"], response_class=StreamingResponse)
async def proxy_mangadex_image(chapter_hash: str, filename: str, base_url: str = None):
    """Proxy for MangaDex images to avoid CORS issues"""
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url parameter is required")
    
    url = f"{base_url}/data/{chapter_hash}/{filename}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True)
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch image from MangaDex")
            
            # Get the content type from the response headers, default to application/octet-stream
            content_type = response.headers.get("content-type", "application/octet-stream")
            
            return StreamingResponse(io.BytesIO(response.content), media_type=content_type)
    except Exception as e:
        logger.error(f"Error proxying MangaDex image: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch image from MangaDex: {str(e)}")

@app.post("/mangadex/process/chapter/{chapter_id}", tags=["mangadex"], response_model=BatchStatus)
async def process_mangadex_chapter(
    chapter_id: str,
    req: Request,
    background_tasks: BackgroundTasks,
    config: str = Form("{}"),
):
    """Automatically download and process a MangaDex chapter"""
    logger.info(f"Processing MangaDex chapter: {chapter_id}")
    
    # Step 1: Get chapter metadata to get manga title and chapter number
    metadata_url = f"https://api.mangadex.org/chapter/{chapter_id}?includes[]=scanlation_group&includes[]=manga&includes[]=user"
    chapter_url = f"https://api.mangadex.org/at-home/server/{chapter_id}?forcePort443=false"
    
    try:
        # Create temporary directory for this chapter in upload-cache
        temp_dir = os.path.join("upload-cache", f"mangadex-{chapter_id}")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Create httpx client with longer timeout
        timeout = httpx.Timeout(30.0, connect=60.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Fetch chapter metadata
            metadata_response = await client.get(metadata_url)
            if metadata_response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Failed to fetch chapter metadata: HTTP {metadata_response.status_code}")
            
            metadata = metadata_response.json()
            if metadata.get("result") != "ok":
                raise HTTPException(status_code=400, detail=f"Failed to fetch chapter metadata: {metadata.get('errors', 'Unknown error')}")
            
            # Extract manga title and chapter number
            manga_title = "Unknown Manga"
            chapter_num = metadata["data"]["attributes"]["chapter"] or "unknown"
            
            for relationship in metadata["data"]["relationships"]:
                if relationship["type"] == "manga":
                    manga_title = relationship["attributes"]["title"].get("en") or next(iter(relationship["attributes"]["title"].values()))
                    break
            
            # Fetch chapter data with image URLs
            chapter_response = await client.get(chapter_url)
            if chapter_response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Failed to fetch chapter data: HTTP {chapter_response.status_code}")
            
            chapter_data = chapter_response.json()
            if chapter_data.get("result") != "ok":
                raise HTTPException(status_code=400, detail=f"Failed to fetch chapter data: {chapter_data.get('errors', 'Unknown error')}")
            
            base_url = chapter_data["baseUrl"]
            chapter_hash = chapter_data["chapter"]["hash"]
            image_files = chapter_data["chapter"]["data"]
            
            logger.info(f"Downloading {len(image_files)} images for {manga_title} Chapter {chapter_num}")
            
            # Step 2: Download all images while preserving order
            successful_downloads = 0
            failed_downloads = 0
            
            # Modified download function to maintain order
            async def download_image(image_file, index):
                # Extract page number from filename (e.g., "r1-..." -> 1)
                try:
                    # Extract the page number from the r{number}- prefix
                    page_number = int(image_file.split('-')[0][1:])
                except (ValueError, IndexError):
                    # Fallback to the index if we can't parse the page number
                    page_number = index + 1
                
                # Create a filename that preserves the original order
                ordered_filename = f"{page_number:03d}_{image_file}"
                image_url = f"{base_url}/data/{chapter_hash}/{image_file}"
                save_path = os.path.join(temp_dir, ordered_filename)
                
                # Retry up to 3 times for each image
                max_retries = 3
                retry_count = 0
                
                while retry_count < max_retries:
                    try:
                        # Download image with increased timeout
                        logger.info(f"Downloading image {index+1}/{len(image_files)}: {image_file} (attempt {retry_count+1})")
                        image_response = await client.get(
                            image_url, 
                            follow_redirects=True,
                            timeout=httpx.Timeout(30.0)  # Individual timeout for each request
                        )
                        
                        if image_response.status_code != 200:
                            logger.warning(f"Failed to download image {image_file}: HTTP {image_response.status_code}")
                            retry_count += 1
                            await asyncio.sleep(1)  # Wait before retrying
                            continue
                        
                        # Save image to disk with ordered filename
                        with open(save_path, "wb") as f:
                            f.write(image_response.content)
                        
                        logger.info(f"Downloaded image {index+1}/{len(image_files)}: {image_file} as {ordered_filename}")
                        return True
                        
                    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError) as e:
                        retry_count += 1
                        logger.warning(f"Timeout when downloading image {image_file} (attempt {retry_count}): {str(e)}")
                        await asyncio.sleep(2)  # Wait longer before retrying
                    except Exception as e:
                        retry_count += 1
                        logger.warning(f"Error downloading image {image_file} (attempt {retry_count}): {str(e)}")
                        await asyncio.sleep(1)
                
                logger.error(f"Failed to download image {image_file} after {max_retries} attempts")
                return False
            
            # Download images sequentially to maintain order
            for i, image_file in enumerate(image_files):
                success = await download_image(image_file, i)
                if success:
                    successful_downloads += 1
                else:
                    failed_downloads += 1
            
            # Check if we have any successful downloads
            if successful_downloads == 0:
                raise HTTPException(status_code=500, detail="Failed to download any images from MangaDex")
            
            logger.info(f"Downloaded {successful_downloads} of {len(image_files)} images successfully (failed: {failed_downloads})")
            
            # Step 3: Create a batch with the manga title and chapter number
            batch_name = f"{manga_title} - Chapter {chapter_num}"
            batch_description = f"MangaDex Chapter {chapter_id} downloaded on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
            
            batch_req = BatchRequest(name=batch_name, description=batch_description)
            batch = task_queue.create_batch(batch_req.name, batch_req.description)
            
            # Save to database
            db_create_batch(batch.batch_id, batch_req.name, batch_req.description)
            
            # Step 4: Add all downloaded images to the batch in correct order
            config_obj = Config.parse_raw(config)
            tasks_added = 0
            
            # Get files and sort them by numeric prefix to ensure correct order
            downloaded_files = sorted(os.listdir(temp_dir))
            
            for i, image_file in enumerate(downloaded_files):
                image_path = os.path.join(temp_dir, image_file)
                
                try:
                    with open(image_path, "rb") as f:
                        img_data = f.read()
                    
                    # Create PIL image
                    img = await to_pil_image(img_data)
                    
                    # Create task with original page number as order_index
                    page_number = int(image_file.split('_')[0])
                    task = QueueElement(req, img, config_obj, priority=1, batch_id=batch.batch_id)
                    
                    # Save to database with order_index to ensure correct order when retrieving
                    save_task(task.task_id, batch.batch_id, "queued", config_obj.dict(), 
                              order_index=page_number, 
                              original_filename=image_file)
                    
                    # Add to queue
                    task_queue.add_task(task)
                    background_tasks.add_task(process_batch_task, task)
                    tasks_added += 1
                    
                    # Remove the file after adding to queue to save space
                    os.remove(image_path)
                except Exception as e:
                    logger.error(f"Error adding image {image_file} to batch: {str(e)}")
            
            # Clean up the temporary directory
            try:
                shutil.rmtree(temp_dir)
                logger.info(f"Removed temporary directory: {temp_dir}")
            except Exception as e:
                logger.warning(f"Could not remove temporary directory {temp_dir}: {str(e)}")
            
            logger.info(f"Created batch {batch.batch_id} with {tasks_added} images (failed downloads: {failed_downloads})")
            
            # Return batch information
            return BatchStatus(**batch.to_dict())
            
    except Exception as e:
        error_message = str(e) or repr(e)
        logger.error(f"Error processing MangaDex chapter: {error_message}")
        raise HTTPException(status_code=500, detail=f"Failed to process MangaDex chapter: {error_message}")

def generate_nonce():
    return secrets.token_hex(16)

def start_translator_client_proc(host: str, port: int, nonce: str, params: Namespace):
    cmds = [
        sys.executable,
        '-m', 'manga_translator',
        'shared',
        '--host', host,
        '--port', str(port),
        '--nonce', nonce,
    ]
    if params.use_gpu:
        cmds.append('--use-gpu')
    if params.use_gpu_limited:
        cmds.append('--use-gpu-limited')
    if params.ignore_errors:
        cmds.append('--ignore-errors')
    if params.verbose:
        cmds.append('--verbose')
    if params.models_ttl:
        cmds.append('--models-ttl=%s' % params.models_ttl)
    if params.pre_dict: 
        cmds.extend(['--pre-dict', params.pre_dict]) 
    if params.post_dict: 
        cmds.extend(['--post-dict', params.post_dict])

    base_path = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(base_path)
    
    # Log startup
    logger.info(f"Starting translator client: {' '.join(cmds)}")
    
    # Set environment variables for connection handling
    if getattr(params, 'use_fixed_ip', False):
        os.environ["USE_FIXED_IP"] = "1"
        os.environ["FIXED_IP"] = params.fixed_ip or "127.0.0.1"
    
    proc = subprocess.Popen(cmds, cwd=parent)
    logger.info(f"Registering local executor at {host}:{port}")
    executor_instances.register(ExecutorInstance(ip=host, port=port))

    def handle_exit_signals(signal, frame):
        logger.info("Received exit signal, shutting down...")
        proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit_signals)
    signal.signal(signal.SIGTERM, handle_exit_signals)

    return proc

def prepare(args):
    global nonce
    if args.nonce is None:
        nonce = os.getenv('MT_WEB_NONCE', generate_nonce())
    else:
        nonce = args.nonce
    
    # Set the host in environment variable for other functions to use
    os.environ["HOST"] = args.host
    
    # Set up CPU core utilization
    import multiprocessing
    import torch
    num_cores = multiprocessing.cpu_count()
    logger.info(f"Detected {num_cores} CPU cores")
    
    # Configure PyTorch to use all available cores
    torch.set_num_threads(num_cores)
    
    # Configure multiprocessing for other operations
    os.environ["OMP_NUM_THREADS"] = str(num_cores)
    os.environ["MKL_NUM_THREADS"] = str(num_cores)
    
    logger.info(f"Application configured to use {num_cores} CPU cores")
        
    # Create required directories with absolute paths
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # /app
    upload_cache_dir = os.path.join(base_dir, "upload-cache")
    
    os.makedirs(upload_cache_dir, exist_ok=True)

    if args.start_instance:
        port = int(os.getenv("PORT", 8000))
        return start_translator_client_proc(args.host, port + 1, nonce, args)
    
    # Clean up old cache files
    if os.path.exists(upload_cache_dir):
        # Just clean files older than 1 day
        now = datetime.datetime.now()
        for root, dirs, files in os.walk(upload_cache_dir):
            for f in files:
                file_path = os.path.join(root, f)
                try:
                    file_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
                    if (now - file_time).days > 1:
                        os.remove(file_path)
                        logger.info(f"Removed old cache file: {file_path}")
                except Exception as e:
                    logger.error(f"Error removing old cache file {file_path}: {str(e)}")
    
    return None

if __name__ == '__main__':
    import uvicorn
    from args import parse_arguments
    
    args = parse_arguments()
    args.start_instance = True
    proc = prepare(args)
    logger.info(f"Server nonce: {nonce}")
    
    port = int(os.getenv("PORT", 8000))
    try:
        # Create an on-disk uvicorn configuration for logging
        uvicorn_config = """
[uvicorn]
host = "{host}"
port = {port}
log_level = "info"
workers = 1
loop = "asyncio"
"""
        with open("uvicorn.conf", "w") as f:
            f.write(uvicorn_config.format(host=args.host, port=args.port))
            
        # Start the server
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except Exception as e:
        logger.error(f"Error starting server: {str(e)}")
        if proc:
            proc.terminate()