import os
import datetime
import json
import logging
import mysql.connector
from mysql.connector import pooling
from typing import Dict, List, Any, Optional

# Configure logging
logger = logging.getLogger("database")

# Database connection parameters from environment variables
DB_HOST = os.getenv('MYSQL_HOST', 'localhost')
DB_PORT = int(os.getenv('MYSQL_PORT', '3306'))
DB_USER = os.getenv('MYSQL_USER', 'root')
DB_PASSWORD = os.getenv('MYSQL_PASSWORD', 'root')
DB_NAME = os.getenv('MYSQL_DATABASE', 'manga_translator')

# Create connection pool
try:
    connection_pool = pooling.MySQLConnectionPool(
        pool_name="manga_translator_pool",
        pool_size=5,
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )
    logger.info(f"Connected to MySQL database at {DB_HOST}:{DB_PORT}")
except Exception as e:
    logger.error(f"Failed to connect to MySQL database: {e}")
    connection_pool = None

def init_database():
    """Initialize database tables if they don't exist"""
    if not connection_pool:
        logger.error("Cannot initialize database: connection pool is None")
        return False
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor()
        
        # Create tasks table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id VARCHAR(50) PRIMARY KEY,
            batch_id VARCHAR(50) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'queued',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP NULL,
            retries INT NOT NULL DEFAULT 0,
            error_message TEXT NULL,
            result_path TEXT NULL,
            config JSON NULL
        )
        """)
        
        # Create batches table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            batch_id VARCHAR(50) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            description TEXT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'created',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP NULL,
            total_tasks INT NOT NULL DEFAULT 0,
            completed_tasks INT NOT NULL DEFAULT 0,
            failed_tasks INT NOT NULL DEFAULT 0
        )
        """)
        
        connection.commit()
        logger.info("Database tables initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        return False
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

# Task operations
def save_task(task_id: str, batch_id: Optional[str] = None, 
              status: str = "queued", config: Optional[Dict] = None) -> bool:
    """Save a new task to the database"""
    if not connection_pool:
        logger.error("Cannot save task: connection pool is None")
        return False
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor()
        
        config_json = json.dumps(config) if config else None
        
        cursor.execute("""
        INSERT INTO tasks (task_id, batch_id, status, config)
        VALUES (%s, %s, %s, %s)
        """, (task_id, batch_id, status, config_json))
        
        connection.commit()
        logger.info(f"Task {task_id} saved to database")
        
        # If this is part of a batch, update the batch's total count
        if batch_id:
            cursor.execute("""
            UPDATE batches SET total_tasks = total_tasks + 1
            WHERE batch_id = %s
            """, (batch_id,))
            connection.commit()
        
        return True
    except Exception as e:
        logger.error(f"Error saving task {task_id}: {e}")
        return False
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

def update_task(task_id: str, status: str, 
                result_path: Optional[str] = None, 
                error_message: Optional[str] = None) -> bool:
    """Update an existing task in the database"""
    if not connection_pool:
        logger.error("Cannot update task: connection pool is None")
        return False
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor()
        
        update_params = {"status": status, "task_id": task_id}
        sql_parts = ["UPDATE tasks SET status = %(status)s"]
        
        if result_path:
            update_params["result_path"] = result_path
            sql_parts.append("result_path = %(result_path)s")
            
        if error_message:
            update_params["error_message"] = error_message
            sql_parts.append("error_message = %(error_message)s")
            
        if status in ["completed", "failed"]:
            sql_parts.append("completed_at = CURRENT_TIMESTAMP")
        
        sql = ", ".join(sql_parts) + " WHERE task_id = %(task_id)s"
        
        cursor.execute(sql, update_params)
        connection.commit()
        
        # If the task is part of a batch, update the batch status
        cursor.execute("SELECT batch_id FROM tasks WHERE task_id = %s", (task_id,))
        result = cursor.fetchone()
        
        if result and result[0]:  # If there's a batch_id
            batch_id = result[0]
            if status == "completed":
                cursor.execute("""
                UPDATE batches SET completed_tasks = completed_tasks + 1
                WHERE batch_id = %s
                """, (batch_id,))
            elif status == "failed":
                cursor.execute("""
                UPDATE batches SET failed_tasks = failed_tasks + 1
                WHERE batch_id = %s
                """, (batch_id,))
            
            # Check if all tasks in the batch are done
            cursor.execute("""
            SELECT total_tasks, completed_tasks, failed_tasks 
            FROM batches WHERE batch_id = %s
            """, (batch_id,))
            batch_data = cursor.fetchone()
            
            if batch_data and (batch_data[1] + batch_data[2] >= batch_data[0]):
                # All tasks are either completed or failed
                cursor.execute("""
                UPDATE batches SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE batch_id = %s
                """, (batch_id,))
            
            connection.commit()
        
        logger.info(f"Task {task_id} updated: {status}")
        return True
    except Exception as e:
        logger.error(f"Error updating task {task_id}: {e}")
        return False
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Get a task by its ID"""
    if not connection_pool:
        logger.error("Cannot get task: connection pool is None")
        return None
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("""
        SELECT * FROM tasks WHERE task_id = %s
        """, (task_id,))
        
        task = cursor.fetchone()
        
        if task and task.get("config"):
            task["config"] = json.loads(task["config"])
            
        return task
    except Exception as e:
        logger.error(f"Error getting task {task_id}: {e}")
        return None
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

def get_recent_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    """Get a list of recent tasks"""
    if not connection_pool:
        logger.error("Cannot get recent tasks: connection pool is None")
        return []
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("""
        SELECT * FROM tasks
        ORDER BY created_at DESC
        LIMIT %s
        """, (limit,))
        
        tasks = cursor.fetchall()
        
        # Parse config JSON
        for task in tasks:
            if task.get("config"):
                task["config"] = json.loads(task["config"])
        
        return tasks
    except Exception as e:
        logger.error(f"Error getting recent tasks: {e}")
        return []
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

# Batch operations
def create_batch(batch_id: str, name: str, description: Optional[str] = None) -> bool:
    """Create a new batch in the database"""
    if not connection_pool:
        logger.error("Cannot create batch: connection pool is None")
        return False
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor()
        
        cursor.execute("""
        INSERT INTO batches (batch_id, name, description, status)
        VALUES (%s, %s, %s, %s)
        """, (batch_id, name, description, "created"))
        
        connection.commit()
        logger.info(f"Batch {batch_id} created: {name}")
        return True
    except Exception as e:
        logger.error(f"Error creating batch {batch_id}: {e}")
        return False
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

def get_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    """Get a batch by its ID"""
    if not connection_pool:
        logger.error("Cannot get batch: connection pool is None")
        return None
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("""
        SELECT * FROM batches WHERE batch_id = %s
        """, (batch_id,))
        
        batch = cursor.fetchone()
        
        if batch:
            # Calculate progress percentage
            if batch["total_tasks"] > 0:
                batch["progress"] = round(
                    (batch["completed_tasks"] + batch["failed_tasks"]) / 
                    batch["total_tasks"] * 100, 2
                )
            else:
                batch["progress"] = 0
                
        return batch
    except Exception as e:
        logger.error(f"Error getting batch {batch_id}: {e}")
        return None
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

def get_batch_tasks(batch_id: str) -> List[Dict[str, Any]]:
    """Get all tasks belonging to a batch"""
    if not connection_pool:
        logger.error("Cannot get batch tasks: connection pool is None")
        return []
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("""
        SELECT * FROM tasks WHERE batch_id = %s
        ORDER BY created_at DESC
        """, (batch_id,))
        
        tasks = cursor.fetchall()
        
        # Parse config JSON
        for task in tasks:
            if task.get("config"):
                task["config"] = json.loads(task["config"])
        
        return tasks
    except Exception as e:
        logger.error(f"Error getting tasks for batch {batch_id}: {e}")
        return []
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

def list_batches(limit: int = 50) -> List[Dict[str, Any]]:
    """List all batches with progress information"""
    if not connection_pool:
        logger.error("Cannot list batches: connection pool is None")
        return []
    
    try:
        connection = connection_pool.get_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("""
        SELECT * FROM batches
        ORDER BY created_at DESC
        LIMIT %s
        """, (limit,))
        
        batches = cursor.fetchall()
        
        # Calculate progress for each batch
        for batch in batches:
            if batch["total_tasks"] > 0:
                batch["progress"] = round(
                    (batch["completed_tasks"] + batch["failed_tasks"]) / 
                    batch["total_tasks"] * 100, 2
                )
            else:
                batch["progress"] = 0
        
        return batches
    except Exception as e:
        logger.error(f"Error listing batches: {e}")
        return []
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()

# Initialize database on module import
init_database()
