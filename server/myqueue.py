import asyncio
import os
import datetime
import uuid
import json
import logging
from typing import List, Optional, Dict, Any
from collections import deque

from PIL import Image
from fastapi import HTTPException
from fastapi.requests import Request

from manga_translator import Config
from server.instance import executor_instances
from server.sent_data_internal import NotifyType

logger = logging.getLogger("myqueue")

class QueueElement:
    def __init__(self, request, image, config, priority=0, batch_id=None, order_index=None):
        self.task_id = uuid.uuid4().hex
        self.request = request
        self.image = image
        self.config = config
        self.result = None
        self.error = None
        self.error_message = None
        self.status = "queued"
        self.priority = priority
        self.batch_id = batch_id
        self.order_index = order_index  # Store order index for sorting
        self.temp_filepath = None
        self.created_at = datetime.datetime.now()  # Add created_at timestamp
        self.result_path = None  # Add result_path attribute
        self.retries = 0  # Initialize retries counter
        self.max_retries = 3  # Maximum number of retries

    def get_image(self) -> Image.Image:
        if isinstance(self.image, str):
            return Image.open(self.image)
        else:
            return self.image

    def __del__(self):
        try:
            self.cleanup_files()
        except Exception as e:
            print(f"Error cleaning up task {self.task_id} during deletion: {str(e)}")

    def cleanup_files(self):
        """Explicitly clean up any temporary files associated with this task"""
        try:
            # Clean up temp file if it exists
            if hasattr(self, 'temp_filepath') and self.temp_filepath and os.path.exists(self.temp_filepath):
                os.remove(self.temp_filepath)
                logger.info(f"Removed temporary file: {self.temp_filepath}")
                self.temp_filepath = None
                
            # Also check for any files in upload-cache with this task_id
            upload_cache_dir = "upload-cache"
            if os.path.exists(upload_cache_dir):
                for filename in os.listdir(upload_cache_dir):
                    if self.task_id in filename:
                        file_path = os.path.join(upload_cache_dir, filename)
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                            logger.info(f"Removed cache file: {file_path}")
        except Exception as e:
            logger.error(f"Error cleaning up files for task {self.task_id}: {str(e)}")

    async def is_client_disconnected(self) -> bool:
        try:
            if await self.request.is_disconnected():
                self.status = "disconnected"
                return True
        except Exception:
            # In case of error assume client is disconnected
            self.status = "disconnected"
            return True
        return False
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert task to dict for API responses and history"""
        return {
            "task_id": self.task_id,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "priority": self.priority,
            "batch_id": self.batch_id,
            "order_index": self.order_index,
            "retries": getattr(self, 'retries', 0),
            "result_path": getattr(self, 'result_path', None),
            "error_message": self.error_message,
            "config": json.loads(self.config.json()) if self.config else None
        }

class BatchJob:
    """Manages a batch of related translation tasks"""
    def __init__(self, name: str, description: Optional[str] = None):
        self.batch_id = str(uuid.uuid4())
        self.name = name
        self.description = description
        self.created_at = datetime.datetime.now()
        self.updated_at = datetime.datetime.now()
        self.status = "created"
        self.total_tasks = 0
        self.completed_tasks = 0
        self.failed_tasks = 0
        self.tasks: List[str] = []  # List of task IDs
        
    def add_task(self, task_id: str):
        """Add a task to this batch"""
        self.tasks.append(task_id)
        self.total_tasks += 1
        self.updated_at = datetime.datetime.now()
        
    def update_status(self):
        """Update batch status based on task statuses"""
        if self.total_tasks == 0:
            # If no tasks, keep as "created"
            self.status = "created"
        elif self.failed_tasks == self.total_tasks:
            self.status = "failed"
        elif self.completed_tasks + self.failed_tasks == self.total_tasks:
            self.status = "completed"
        elif self.completed_tasks > 0 or self.failed_tasks > 0:
            self.status = "in_progress"
        else:
            self.status = "queued"
        self.updated_at = datetime.datetime.now()
        
    def get_sorted_tasks(self, task_list):
        """Sort tasks by order_index if available"""
        # First prioritize tasks with order_index
        tasks_with_order = []
        tasks_without_order = []
        
        for task in task_list:
            if hasattr(task, 'order_index') and task.order_index is not None:
                tasks_with_order.append(task)
            else:
                tasks_without_order.append(task)
        
        # Sort tasks with order_index
        sorted_tasks = sorted(tasks_with_order, key=lambda x: x.order_index)
        
        # Append tasks without order_index
        sorted_tasks.extend(tasks_without_order)
        
        return sorted_tasks
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert batch to dict for API responses"""
        return {
            "batch_id": self.batch_id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status": self.status,
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "progress": (self.completed_tasks + self.failed_tasks) / max(self.total_tasks, 1) * 100
        }

class TaskHistory:
    """Stores task history and results using database"""
    def __init__(self, max_history: int = 1000):
        self.history: Dict[str, QueueElement] = {}
        self.batch_jobs: Dict[str, BatchJob] = {}
        self.max_history = max_history
        
        # Create results directory if it doesn't exist
        os.makedirs("upload-cache", exist_ok=True)
        
    def add_task(self, task: QueueElement):
        """Add task to history"""
        self.history[task.task_id] = task
        
        # Remove oldest items if we exceed max history
        if len(self.history) > self.max_history:
            oldest_tasks = sorted(
                self.history.items(), 
                key=lambda x: x[1].created_at
            )[:len(self.history) - self.max_history]
            
            for task_id, task in oldest_tasks:
                # Clean up task files from cache
                if task.image_path and os.path.exists(task.image_path):
                    try:
                        os.remove(task.image_path)
                    except Exception:
                        pass
                        
                del self.history[task_id]
    
    def create_batch(self, name: str, description: Optional[str] = None) -> BatchJob:
        """Create a new batch job"""
        batch = BatchJob(name, description)
        self.batch_jobs[batch.batch_id] = batch
        return batch
    
    def get_batch(self, batch_id: str) -> Optional[BatchJob]:
        """Get a batch job by ID"""
        return self.batch_jobs.get(batch_id)
    
    def update_batch(self, batch_id: str, task_id: str, status: str):
        """Update batch status when a task completes or fails"""
        batch = self.batch_jobs.get(batch_id)
        if not batch:
            return
            
        if status == "completed":
            batch.completed_tasks += 1
        elif status in ("failed", "error"):
            batch.failed_tasks += 1
            
        batch.update_status()
    
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task history by ID"""
        task = self.history.get(task_id)
        if task:
            return task.to_dict()
        return None
    
    def get_recent_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent task history - use database implementation instead"""
        from server.database import get_recent_tasks
        try:
            return get_recent_tasks(limit)
        except Exception as e:
            print(f"Error getting tasks from database, falling back to memory: {e}")
            # Fallback to in-memory
            recent_tasks = sorted(
                self.history.values(), 
                key=lambda x: x.created_at, 
                reverse=True
            )[:limit]
            
            return [task.to_dict() for task in recent_tasks]
    
    def get_batch_tasks(self, batch_id: str) -> List[Dict[str, Any]]:
        """Get all tasks in a batch - use database implementation instead"""
        from server.database import get_batch_tasks
        try:
            return get_batch_tasks(batch_id)
        except Exception as e:
            print(f"Error getting batch tasks from database, falling back to memory: {e}")
            # Fallback to in-memory
            batch = self.batch_jobs.get(batch_id)
            if not batch:
                return []
                
            tasks = [
                self.get_task(task_id) for task_id in batch.tasks
                if task_id in self.history
            ]
            
            # Sort tasks by order_index if available
            return sorted(tasks, key=lambda x: x.get('order_index', float('inf')))
    
    def list_batches(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List recent batch jobs - use database implementation instead"""
        from server.database import list_batches
        try:
            return list_batches(limit)
        except Exception as e:
            print(f"Error listing batches from database, falling back to memory: {e}")
            # Fallback to in-memory
            recent_batches = sorted(
                self.batch_jobs.values(), 
                key=lambda x: x.updated_at, 
                reverse=True
            )[:limit]
            
            return [batch.to_dict() for batch in recent_batches]

class TaskQueue:
    def __init__(self):
        self.queue: List[QueueElement] = []
        self.queue_event: asyncio.Event = asyncio.Event()
        self.task_history = TaskHistory()
        self.periodic_cleanup_task = None
        self.lock = asyncio.Lock()
        self.cleanup_interval = 3600  # 1 hour
    
    def _start_periodic_cleanup(self):
        """Start periodic cleanup of orphaned tasks"""
        if self.periodic_cleanup_task is None:
            self.periodic_cleanup_task = asyncio.create_task(self._run_periodic_cleanup())
    
    async def start_background_tasks(self):
        """Start background tasks like periodic cleanup."""
        self._start_periodic_cleanup()

    async def _run_periodic_cleanup(self):
        """Periodically clean up disconnected tasks and orphaned files"""
        while True:
            await asyncio.sleep(60)  # Run every minute
            try:
                await self.cleanup_disconnected_tasks()
                self.cleanup_upload_cache()
            except Exception as e:
                print(f"Error in periodic cleanup: {str(e)}")
    
    async def cleanup_disconnected_tasks(self):
        """Remove disconnected tasks from the queue"""
        async with self.lock:
            original_length = len(self.queue)
            self.queue = [task for task in self.queue if not await task.is_client_disconnected()]
            
            if len(self.queue) < original_length:
                print(f"Cleaned up {original_length - len(self.queue)} disconnected tasks")
                await self.update_event()

    def cleanup_upload_cache(self):
        """Clean up orphaned files in upload-cache directory"""
        try:
            upload_cache_dir = "upload-cache"
            if not os.path.exists(upload_cache_dir):
                return
                
            # Get all task IDs from current queue
            active_task_ids = set()
            for task in self.queue:
                active_task_ids.add(task.task_id)
                
            # Add task IDs from recent tasks that aren't completed or failed
            for task in self.task_history.history.values():
                if task.status not in ["completed", "failed"]:
                    active_task_ids.add(task.task_id)
            
            # Remove files that don't belong to active tasks
            removed_count = 0
            for filename in os.listdir(upload_cache_dir):
                should_keep = False
                file_path = os.path.join(upload_cache_dir, filename)
                
                # Skip directories like mangadex-* which might still be in use
                if os.path.isdir(file_path):
                    continue
                    
                # Check if this file belongs to an active task
                for task_id in active_task_ids:
                    if task_id in filename:
                        should_keep = True
                        break
                        
                # Remove the file if it's not needed
                if not should_keep and os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                        removed_count += 1
                    except Exception as e:
                        logger.error(f"Error removing file {file_path}: {str(e)}")
            
            if removed_count > 0:
                logger.info(f"Cleaned up {removed_count} orphaned files from upload-cache")
        except Exception as e:
            logger.error(f"Error in cleanup_upload_cache: {str(e)}")

    def add_task(self, task: QueueElement):
        """Add a task to the queue"""
        self.queue.append(task)
        # Also add to history
        self.task_history.add_task(task)
        
        # If task is part of a batch, update the batch
        if task.batch_id:
            batch = self.task_history.get_batch(task.batch_id)
            if batch:
                batch.add_task(task.task_id)

    def get_pos(self, task: QueueElement) -> Optional[int]:
        """Get position of task in queue"""
        try:
            return self.queue.index(task)
        except ValueError:
            return None
            
    def get_task_by_id(self, task_id: str) -> Optional[QueueElement]:
        """Get a task by ID"""
        for task in self.queue:
            if task.task_id == task_id:
                return task
        return None
    
    async def update_event(self):
        """Update queue event to notify waiters"""
        # Sort queue by priority (higher first)
        self.queue.sort(key=lambda x: (-x.priority, x.created_at))
        
        self.queue_event.set()
        self.queue_event.clear()

    async def remove(self, task: QueueElement):
        """Remove a task from the queue"""
        try:
            self.queue.remove(task)
            await self.update_event()
        except ValueError:
            pass  # Task not in queue

    async def wait_for_event(self):
        """Wait for a queue event"""
        await self.queue_event.wait()
        
    def create_batch(self, name: str, description: Optional[str] = None) -> BatchJob:
        """Create a new batch job"""
        return self.task_history.create_batch(name, description)
    
    def get_batch(self, batch_id: str) -> Optional[BatchJob]:
        """Get a batch job"""
        return self.task_history.get_batch(batch_id)
        
    def get_recent_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent tasks"""
        return self.task_history.get_recent_tasks(limit)
    
    def get_batch_tasks(self, batch_id: str) -> List[Dict[str, Any]]:
        """Get tasks in a batch"""
        return self.task_history.get_batch_tasks(batch_id)
    
    def list_batches(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List batch jobs"""
        return self.task_history.list_batches(limit)

# Global task queue instance
task_queue = TaskQueue()

# Make sure BatchJob is exported by adding it to the global namespace
__all__ = ['task_queue', 'QueueElement', 'BatchJob', 'wait_in_queue']

# Export BatchJob at the module level
BatchJob = BatchJob  # This makes the class available for import

async def wait_in_queue(task: QueueElement, notify: NotifyType):
    """Will get task position report it. If its in the range of translators then it will try to acquire an instance(blocking) and sent a task to it. when done the item will be removed from the queue and result will be returned"""
    max_attempts = 3
    attempt = 0
    
    while True:
        try:
            queue_pos = task_queue.get_pos(task)
            
            # If task no longer in queue
            if queue_pos is None:
                if notify:
                    return
                else:
                    task.status = "error"
                    task.error_message = "Task no longer in queue"
                    # Clean up cache files
                    task.cleanup_files()
                    raise HTTPException(500, detail="Task was removed from queue")
            
            # Update client on queue position if streaming
            if notify:
                notify(3, str(queue_pos).encode('utf-8'))
            
            # Check if task can be processed (there's a free executor)
            if queue_pos < executor_instances.free_executors():
                # Check if client is still connected
                if await task.is_client_disconnected():
                    await task_queue.update_event()
                    # Clean up cache files for disconnected clients
                    task.cleanup_files()
                    if notify:
                        return
                    else:
                        task.status = "disconnected"
                        raise HTTPException(500, detail="User is no longer connected")

                # Get an executor instance
                instance = await executor_instances.find_executor()
                
                # Update task status
                task.status = "processing"
                
                # Remove from queue
                await task_queue.remove(task)
                
                # Notify client that task is being processed
                if notify:
                    notify(4, b"")
                
                try:
                    # Process task
                    if notify:
                        # Streaming response
                        await instance.sent_stream(task.get_image(), task.config, notify)
                        task.status = "completed"
                        
                        # Update batch if needed
                        if task.batch_id:
                            task_queue.task_history.update_batch(task.batch_id, task.task_id, "completed")
                        
                        # Clean up upload-cache file after successful processing
                        task.cleanup_files()
                    else:
                        # Normal response
                        result = await instance.sent(task.get_image(), task.config)
                        
                        # Save result
                        result_dir = os.path.join("history", "results", datetime.datetime.now().strftime("%Y%m%d"))
                        os.makedirs(result_dir, exist_ok=True)
                        
                        result_path = os.path.join(result_dir, f"{task.task_id}.png")
                        result.result.save(result_path)
                        task.result_path = result_path
                        task.status = "completed"
                        
                        # Update batch if needed
                        if task.batch_id:
                            task_queue.task_history.update_batch(task.batch_id, task.task_id, "completed")
                        
                        # Clean up upload-cache file after successful processing
                        task.cleanup_files()
                        
                        return result
                
                except Exception as e:
                    # Handle task processing error
                    task.retries += 1
                    task.error_message = str(e)
                    
                    if task.retries >= task.max_retries:
                        task.status = "failed"
                        if task.batch_id:
                            task_queue.task_history.update_batch(task.batch_id, task.task_id, "failed")
                        
                        # Clean up upload-cache file after final failure
                        task.cleanup_files()
                        
                        if notify:
                            notify(2, f"Failed to process task after {task.retries} attempts: {str(e)}".encode('utf-8'))
                            return
                        else:
                            raise HTTPException(500, detail=f"Task processing failed: {str(e)}")
                    else:
                        # Re-queue the task for retry
                        task.status = "retrying"
                        task_queue.add_task(task)
                        await task_queue.update_event()
                        
                        if notify:
                            notify(2, f"Retrying task ({task.retries}/{task.max_retries}): {str(e)}".encode('utf-8'))
                        else:
                            raise HTTPException(503, detail=f"Task processing failed, retrying ({task.retries}/{task.max_retries})")
                finally:
                    # Free the executor instance
                    await executor_instances.free_executor(instance)
            else:
                # Wait for a queue event
                await task_queue.wait_for_event()
                
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
                
            attempt += 1
            if attempt >= max_attempts:
                task.status = "error"
                task.error_message = f"Error in queue processing: {str(e)}"
                
                if task.batch_id:
                    task_queue.task_history.update_batch(task.batch_id, task.task_id, "error")
                
                # Clean up upload-cache file after critical error
                task.cleanup_files()
                
                if notify:
                    notify(2, f"Critical error: {str(e)}".encode('utf-8'))
                    return
                else:
                    raise HTTPException(500, detail=f"Critical error in queue processing: {str(e)}")
            else:
                # Wait a bit before retrying
                await asyncio.sleep(1)