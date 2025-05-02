document.addEventListener('DOMContentLoaded', () => {
    // Init
    loadBatchFilter();
    refreshHistory();
    
    // Event listeners
    document.getElementById('refresh-history').addEventListener('click', refreshHistory);
    document.getElementById('batch-filter').addEventListener('change', refreshHistory);
    document.getElementById('status-filter').addEventListener('change', refreshHistory);
    
    // Check URL for batch parameter
    const urlParams = new URLSearchParams(window.location.search);
    const batchId = urlParams.get('batch');
    if (batchId) {
        document.getElementById('batch-filter').value = batchId;
        refreshHistory();
    }
    
    // Modal
    const modal = document.getElementById('image-modal');
    const closeBtn = document.querySelector('.close');
    
    closeBtn.addEventListener('click', () => {
        modal.style.display = 'none';
    });
    
    window.addEventListener('click', (event) => {
        if (event.target == modal) {
            modal.style.display = 'none';
        }
    });
});

async function loadBatchFilter() {
    try {
        const response = await fetch('/batch');
        if (!response.ok) {
            throw new Error('Failed to fetch batches');
        }
        
        const batches = await response.json();
        
        const selector = document.getElementById('batch-filter');
        
        // Save current selection
        const currentValue = selector.value;
        
        // Clear options except first
        while (selector.options.length > 1) {
            selector.remove(1);
        }
        
        // Add batches
        batches.forEach(batch => {
            const option = document.createElement('option');
            option.value = batch.batch_id;
            option.textContent = batch.name;
            selector.appendChild(option);
        });
        
        // Restore selection if possible
        if (currentValue) {
            for (let i = 0; i < selector.options.length; i++) {
                if (selector.options[i].value === currentValue) {
                    selector.selectedIndex = i;
                    break;
                }
            }
        }
    } catch (error) {
        console.error('Error loading batches:', error);
    }
}

async function refreshHistory() {
    const batchId = document.getElementById('batch-filter').value;
    const status = document.getElementById('status-filter').value;
    
    try {
        let url = '/history';
        if (batchId) {
            url = `/batch/${batchId}/tasks`;
        }
        
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error('Failed to fetch history');
        }
        
        let tasks = await response.json();
        
        // Filter by status if selected
        if (status) {
            tasks = tasks.filter(task => task.status === status);
        }
        
        const container = document.getElementById('history-container');
        container.innerHTML = '';
        
        if (tasks.length === 0) {
            container.innerHTML = '<p>No tasks found</p>';
            return;
        }
        
        tasks.forEach(task => {
            const card = document.createElement('div');
            card.className = 'task-card';
            
            // Add status class
            card.classList.add(`status-${task.status}`);
            
            const date = new Date(task.created_at);
            const formattedDate = date.toLocaleString();
            
            card.innerHTML = `
                <h3>Task: ${task.task_id.substring(0, 8)}...</h3>
                <div class="task-info">
                    <p><strong>Created:</strong> ${formattedDate}</p>
                    <p><strong>Status:</strong> ${task.status}</p>
                    ${task.batch_id ? `<p><strong>Batch:</strong> ${task.batch_id.substring(0, 8)}...</p>` : ''}
                    ${task.retries > 0 ? `<p><strong>Retries:</strong> ${task.retries}</p>` : ''}
                </div>
                ${task.error_message ? `<div class="error-message"><p><strong>Error:</strong> ${task.error_message}</p></div>` : ''}
                ${task.result_path ? `<button class="view-result" data-path="${task.result_path}">View Result</button>` : ''}
            `;
            
            container.appendChild(card);
            
            // Add event listener to view result button if present
            const viewBtn = card.querySelector('.view-result');
            if (viewBtn) {
                viewBtn.addEventListener('click', () => {
                    const modal = document.getElementById('image-modal');
                    const modalImg = document.getElementById('modal-image');
                    const modalTitle = document.getElementById('modal-title');
                    
                    modalTitle.textContent = `Result for Task: ${task.task_id.substring(0, 8)}...`;
                    
                    // Get path to image from data attribute
                    const path = viewBtn.getAttribute('data-path');
                    
                    // Construct URL to fetch the image
                    modalImg.src = `/static/results/${path}`;
                    
                    modal.style.display = 'block';
                });
            }
        });
    } catch (error) {
        console.error('Error refreshing history:', error);
        document.getElementById('history-container').innerHTML = `<p>Error loading history: ${error.message}</p>`;
    }
}
