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
from server.storage import save_result_image
from server.streaming import notify, stream
# Import database functions
from server.database import save_task, update_task, get_task, get_recent_tasks
from server.database import create_batch as db_create_batch
from server.database import get_batch as db_get_batch
from server.database import get_batch_tasks as db_get_batch_tasks
from server.database import list_batches as db_list_batches

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

# Pastikan folder static ada sebelum mount
# Ensure the static directory exists
static_dir = Path(__file__).parent / "static"
os.makedirs(static_dir, exist_ok=True)

# Log the static directory path for debugging
logger.info(f"Static directory path: {static_dir.resolve()}")

# Serve static files for UI using @app.get
@app.get("/static/{file_path:path}", tags=["static"])
async def serve_static(file_path: str):
    file_location = static_dir / file_path
    if not file_location.exists() or not file_location.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Determine the correct media type based on the file extension
    if file_location.suffix in [".png", ".jpg", ".jpeg", ".gif"]:
        media_type = f"image/{file_location.suffix.lstrip('.')}"
    elif file_location.suffix == ".css":
        media_type = "text/css"
    elif file_location.suffix == ".js":
        media_type = "application/javascript"
    else:
        media_type = "application/octet-stream"
    
    return StreamingResponse(file_location.open("rb"), media_type=media_type)

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
        # Save result to R2 storage
        result_path = save_result_image(result.result, task.task_id)
        task.result_path = result_path
        task.status = "completed"
        
        # Update database with completed status and result path
        update_task(task.task_id, "completed", result_path=result_path)
        
        # Clean up cache files
        task.cleanup_files()
    except Exception as e:
        error_message = f"Error processing task {task.task_id}: {str(e)}"
        task.status = "failed"
        task.error_message = error_message
        logger.error(error_message)
        
        # Update database with error message
        update_task(task.task_id, "failed", error_message=error_message)
        
        # Clean up cache files even on error
        task.cleanup_files()
    finally:
        if instance:
            await executor_instances.free_executor(instance)

# Batch processing with database storage
async def process_batch_task(task: QueueElement):
    from server.instance import executor_instances
    try:
        instance = await executor_instances.find_executor()
        task.status = "processing"
        # Update database status
        update_task(task.task_id, "processing")
        
        result = await instance.sent(task.get_image(), task.config)
        # Save result to R2 storage
        result_path = save_result_image(result.result, task.task_id)
        task.result_path = result_path
        task.status = "completed"
        
        # Update database with completed status and result path
        update_task(task.task_id, "completed", result_path=result_path)
        
        # Clean up cache files after successful processing
        task.cleanup_files()
    except Exception as e:
        error_message = str(e)
        task.status = "failed"
        task.error_message = error_message
        
        # Update database with error message
        update_task(task.task_id, "failed", error_message=error_message)
        
        # Clean up cache files even on error
        task.cleanup_files()
    finally:
        await executor_instances.free_executor(instance)

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
    task = task_queue.get_task(task_id)
    if not task:
        raise HTTPException(404, detail=f"Task {task_id} not found")
    
    response = {
        "task_id": task.task_id,
        "status": task.status,
        "created_at": task.created_at,
        "queue_position": task_queue.get_pos(task) if task.status == "queued" else None,
        "retries": task.retries,
    }
    
    if task.error_message:
        response["error_message"] = task.error_message
    
    if task.result_path:
        response["result_url"] = f"/static/results/{task.result_path}"
    
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

@app.get("/", response_class=HTMLResponse,tags=["ui"])
async def index() -> HTMLResponse:
    script_directory = Path(__file__).parent
    html_file = script_directory / "index.html"
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
        # Check if the translator client is already running
        for instance in executor_instances.list:
            if instance.ip == "127.0.0.1" and instance.port == port:
                logger.info(f"Translator client already running on port {port}")
                return {"message": f"Translator client already running on port {port}"}
        
        # Start a new translator client process
        start_translator_client_proc("127.0.0.1", port, nonce, Namespace())
        logger.info(f"Translator client started on port {port}")
        return {"message": f"Translator client started on port {port}"}
    except Exception as e:
        logger.error(f"Failed to start translator client: {e}")
        return {"error": "Failed to start translator client"}

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
    history_dir = os.path.join(base_dir, "history")
    results_dir = os.path.join(history_dir, "results")  # /app/history/results
    
    os.makedirs(upload_cache_dir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)
    
    # Create static directory if it doesn't exist
    server_dir = os.path.dirname(os.path.abspath(__file__))  # /app/server
    static_dir = os.path.join(server_dir, "static")
    os.makedirs(static_dir, exist_ok=True)
    
    # Define the static results directory
    static_results_dir = os.path.join(static_dir, "results")
    
    # Create symbolic link for results in static folder (fix: always recreate if missing or broken)
    # Ensure the symlink points to the correct results directory
    if os.path.islink(static_results_dir) or os.path.exists(static_results_dir):
        try:
            if os.path.islink(static_results_dir):
                os.unlink(static_results_dir)
            else:
                shutil.rmtree(static_results_dir, ignore_errors=True)
            logger.info(f"Removed existing symlink or directory: {static_results_dir}")
        except Exception as e:
            logger.error(f"Error removing existing symlink or directory {static_results_dir}: {str(e)}")
    
    try:
        os.symlink(os.path.abspath(results_dir), static_results_dir, target_is_directory=True)
        logger.info(f"Created symlink: {static_results_dir} -> {os.path.abspath(results_dir)}")
    except Exception as e:
        logger.error(f"Error creating symlink for results: {str(e)}")
        # As a fallback, copy the results directory
        try:
            shutil.copytree(results_dir, static_results_dir, dirs_exist_ok=True)
            logger.info(f"Copied directory: {results_dir} -> {static_results_dir}")
        except Exception as copy_e:
            logger.error(f"Error copying directory: {copy_e}")
    static_results_dir = os.path.join(static_dir, "results")

    # Log paths for debugging
    logger.info(f"Results directory: {os.path.abspath(results_dir)}")
    logger.info(f"Static results directory: {os.path.abspath(static_results_dir)}")

    # Remove broken symlink or folder if exists
    if os.path.islink(static_results_dir):
        try:
            os.unlink(static_results_dir)
            logger.info(f"Removed existing symlink: {static_results_dir}")
        except Exception as e:
            logger.error(f"Error removing symlink {static_results_dir}: {str(e)}")
    elif os.path.exists(static_results_dir):
        try:
            shutil.rmtree(static_results_dir, ignore_errors=True)
            logger.info(f"Removed existing directory: {static_results_dir}")
        except Exception as e:
            logger.error(f"Error removing directory {static_results_dir}: {str(e)}")

    # Create a fresh symlink
    try:
        os.symlink(os.path.abspath(results_dir), static_results_dir, target_is_directory=True)
        logger.info(f"Created symlink: {static_results_dir} -> {os.path.abspath(results_dir)}")
    except Exception as e:
        logger.error(f"Error creating symlink for results: {str(e)}")
        
        # Copy files instead if symlink fails
        try:
            shutil.copytree(results_dir, static_results_dir, dirs_exist_ok=True)
            logger.info(f"Copied directory: {results_dir} -> {static_results_dir}")
        except Exception as copy_e:
            logger.error(f"Error copying directory: {copy_e}")
    
    if args.start_instance:
        port = int(os.getenv("PORT", 8000))
        return start_translator_client_proc("127.0.0.1", port + 1, nonce, args)
    
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