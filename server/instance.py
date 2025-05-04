from asyncio import Event, Lock
from typing import List, Optional
import asyncio
import time
import logging
import socket

from PIL import Image
from pydantic import BaseModel

from manga_translator import Config
from server.sent_data_internal import fetch_data_stream, NotifyType, fetch_data

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("instance_manager")

class ExecutorInstance(BaseModel):
    ip: str
    port: int
    busy: bool = False
    last_error_time: Optional[float] = None
    consecutive_errors: int = 0
    max_retries: int = 3
    backoff_factor: float = 1.5
    health_check_interval: int = 60  # seconds

    def free_executor(self):
        self.busy = False

    async def _check_connection(self) -> bool:
        """Check if the instance is reachable"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((self.ip, self.port))
            sock.close()
            
            # If connecting to a non-localhost address fails, try with localhost
            if result != 0 and self.ip != "127.0.0.1" and self.ip != "localhost":
                # This helps with the case where external IP is used but internal access is needed
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(("127.0.0.1", self.port))
                sock.close()
                if result == 0:
                    # If localhost connection works, update the IP for future connections
                    logger.info(f"Updating instance IP from {self.ip} to 127.0.0.1 for local access")
                    self.ip = "127.0.0.1"
                    return True
            
            return result == 0
        except Exception as e:
            logger.warning(f"Health check failed for {self.ip}:{self.port} - {str(e)}")
            return False

    async def sent(self, image: Image, config: Config):
        """Send task to executor with retry mechanism"""
        retry_count = 0
        last_exception = None
        
        while retry_count <= self.max_retries:
            try:
                if retry_count > 0:
                    # Check connection before retry
                    if not await self._check_connection():
                        wait_time = self.backoff_factor ** retry_count
                        logger.warning(f"Instance {self.ip}:{self.port} not reachable, waiting {wait_time}s before retry")
                        await asyncio.sleep(wait_time)
                        
                url = f"http://{self.ip}:{self.port}/simple_execute/translate"
                logger.info(f"Sending request to {url}, attempt {retry_count+1}/{self.max_retries+1}")
                result = await fetch_data(url, image, config)
                
                # Reset error counters on success
                self.consecutive_errors = 0
                self.last_error_time = None
                return result
                
            except Exception as e:
                last_exception = e
                retry_count += 1
                self.consecutive_errors += 1
                self.last_error_time = time.time()
                
                if retry_count <= self.max_retries:
                    wait_time = self.backoff_factor ** retry_count
                    logger.warning(f"Attempt {retry_count} failed, retrying in {wait_time} seconds: {str(e)}")
                    await asyncio.sleep(wait_time)
        
        # If we get here, all retries failed
        logger.error(f"All retries failed for {self.ip}:{self.port}: {str(last_exception)}")
        raise last_exception

    async def sent_stream(self, image: Image, config: Config, sender: NotifyType):
        """Send stream task to executor with retry mechanism"""
        retry_count = 0
        last_exception = None
        
        while retry_count <= self.max_retries:
            try:
                if retry_count > 0:
                    # Check connection before retry
                    if not await self._check_connection():
                        wait_time = self.backoff_factor ** retry_count
                        logger.warning(f"Instance {self.ip}:{self.port} not reachable, waiting {wait_time}s before retry")
                        await asyncio.sleep(wait_time)
                        
                url = f"http://{self.ip}:{self.port}/execute/translate"
                logger.info(f"Sending stream request to {url}, attempt {retry_count+1}/{self.max_retries+1}")
                await fetch_data_stream(url, image, config, sender)
                
                # Reset error counters on success
                self.consecutive_errors = 0
                self.last_error_time = None
                return
                
            except Exception as e:
                last_exception = e
                retry_count += 1
                self.consecutive_errors += 1
                self.last_error_time = time.time()
                
                if retry_count <= self.max_retries:
                    wait_time = self.backoff_factor ** retry_count
                    logger.warning(f"Stream attempt {retry_count} failed, retrying in {wait_time} seconds: {str(e)}")
                    await asyncio.sleep(wait_time)
                    # Notify client about retry attempt
                    if sender:
                        sender(2, f"Connection error, retrying ({retry_count}/{self.max_retries})...".encode('utf-8'))
        
        # If we get here, all retries failed
        error_msg = f"All connection attempts to translator instance at {self.ip}:{self.port} failed"
        logger.error(f"{error_msg}: {str(last_exception)}")
        if sender:
            sender(2, error_msg.encode('utf-8'))
        raise last_exception

class Executors:
    def __init__(self):
        self.list: List[ExecutorInstance] = []
        self.lock: Lock = Lock()
        self.event = Event()
        self.health_check_task = None

    def register(self, instance: ExecutorInstance):
        self.list.append(instance)
        logger.info(f"Registered new executor instance at {instance.ip}:{instance.port}")
        # Health check task will be started explicitly from FastAPI startup event

    async def start_background_tasks(self):
        if self.health_check_task is None:
            self.health_check_task = asyncio.create_task(self._run_health_checks())

    def free_executors(self) -> int:
        return len([item for item in self.list if not item.busy])

    async def _run_health_checks(self):
        """Periodically check all instances for health"""
        while True:
            await asyncio.sleep(60)  # Run every minute
            for instance in self.list:
                if not instance.busy:
                    try:
                        is_healthy = await instance._check_connection()
                        if not is_healthy:
                            logger.warning(f"Health check failed for {instance.ip}:{instance.port}")
                    except Exception as e:
                        logger.error(f"Error during health check: {str(e)}")

    async def _find_instance(self):
        while True:
            # Sort by least errors first, then by not busy
            sorted_instances = sorted(self.list, key=lambda x: (x.busy, x.consecutive_errors))
            instance = next((x for x in sorted_instances if not x.busy), None)
            
            if instance is not None:
                return instance
                
            logger.warning("No free executor instances available, waiting...")
            await self.event.wait()

    async def find_executor(self, max_retries=5, initial_delay=1.5, backoff_factor=1.5):
        """Find an available executor instance with retry logic."""
        for attempt in range(1, max_retries + 1):
            for instance in self.list:
                if not instance.busy:
                    try:
                        is_healthy = await instance._check_connection()
                        if is_healthy:
                            instance.busy = True
                            logger.info(f"Allocated executor instance at {instance.ip}:{instance.port}")
                            return instance
                    except Exception as e:
                        logger.warning(f"Instance {instance.ip}:{instance.port} not reachable, "
                                       f"attempt {attempt}/{max_retries}: {str(e)}")
            
            # If no instance is available, wait before retrying
            if attempt < max_retries:
                delay = initial_delay * (backoff_factor ** (attempt - 1))
                logger.warning(f"Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
        
        raise RuntimeError("No available translator instances after retries.")

    async def free_executor(self, instance: ExecutorInstance):
        from server.myqueue import task_queue
        instance.free_executor()
        logger.info(f"Freed executor instance at {instance.ip}:{instance.port}")
        self.event.set()
        self.event.clear()
        await task_queue.update_event()

executor_instances: Executors = Executors()
