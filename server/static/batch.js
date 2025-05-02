document.addEventListener('DOMContentLoaded', () => {
    // Init
    refreshBatchList();
    loadBatchSelector();
    
    // Event listeners
    document.getElementById('create-batch-form').addEventListener('submit', createBatch);
    document.getElementById('upload-form').addEventListener('submit', uploadImages);
    document.getElementById('refresh-batches').addEventListener('click', loadBatchSelector);
    document.getElementById('refresh-batch-list').addEventListener('click', refreshBatchList);
    document.getElementById('batch-selector').addEventListener('change', toggleUploadForm);
});

function toggleUploadForm() {
    const batchSelector = document.getElementById('batch-selector');
    const uploadForm = document.getElementById('upload-form');
    
    if (batchSelector.value) {
        uploadForm.style.display = 'block';
    } else {
        uploadForm.style.display = 'none';
    }
}

async function createBatch(event) {
    event.preventDefault();
    
    const name = document.getElementById('batch-name').value;
    const description = document.getElementById('batch-description').value;
    
    try {
        const response = await fetch('/batch/create', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ name, description })
        });
        
        if (!response.ok) {
            throw new Error('Failed to create batch: ' + await response.text());
        }
        
        const batch = await response.json();
        alert(`Batch created: ${batch.name} (ID: ${batch.batch_id})`);
        
        // Reset form
        document.getElementById('create-batch-form').reset();
        
        // Refresh batch list
        refreshBatchList();
        loadBatchSelector();
    } catch (error) {
        console.error('Error creating batch:', error);
        alert('Error creating batch: ' + error.message);
    }
}

async function uploadImages(event) {
    event.preventDefault();
    
    const batchId = document.getElementById('batch-selector').value;
    const files = document.getElementById('image-files').files;
    const config = document.getElementById('config').value;
    
    if (!batchId) {
        alert('Please select a batch');
        return;
    }
    
    if (files.length === 0) {
        alert('Please select at least one image');
        return;
    }
    
    const formData = new FormData();
    for (let i = 0; i < files.length; i++) {
        formData.append('images', files[i]);
    }
    formData.append('config', config);
    
    try {
        const response = await fetch(`/batch/${batchId}/add-multiple`, {
            method: 'POST',
            body: formData
        });
        
        if (!response.ok) {
            throw new Error('Failed to upload images: ' + await response.text());
        }
        
        const result = await response.json();
        alert(`Uploaded ${result.length} images to batch ${batchId}`);
        
        // Reset form
        document.getElementById('upload-form').reset();
        document.getElementById('config').value = '{}';
        
        // Refresh batch list
        refreshBatchList();
    } catch (error) {
        console.error('Error uploading images:', error);
        alert('Error uploading images: ' + error.message);
    }
}

async function loadBatchSelector() {
    try {
        const response = await fetch('/batch');
        if (!response.ok) {
            throw new Error('Failed to fetch batches');
        }
        
        const batches = await response.json();
        
        const selector = document.getElementById('batch-selector');
        
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
            option.textContent = `${batch.name} (${batch.status}, ${Math.round(batch.progress)}%)`;
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
        
        toggleUploadForm();
    } catch (error) {
        console.error('Error loading batches:', error);
        alert('Error loading batches: ' + error.message);
    }
}

async function refreshBatchList() {
    try {
        const response = await fetch('/batch');
        if (!response.ok) {
            throw new Error('Failed to fetch batches');
        }
        
        const batches = await response.json();
        
        const container = document.getElementById('batch-container');
        container.innerHTML = '';
        
        if (batches.length === 0) {
            container.innerHTML = '<p>No batch jobs found</p>';
            return;
        }
        
        batches.forEach(batch => {
            const card = document.createElement('div');
            card.className = 'batch-card';
            
            // Add status class
            card.classList.add(`status-${batch.status}`);
            
            card.innerHTML = `
                <h3>${batch.name}</h3>
                <p>${batch.description || 'No description'}</p>
                <div class="batch-info">
                    <p><strong>ID:</strong> ${batch.batch_id}</p>
                    <p><strong>Status:</strong> ${batch.status}</p>
                    <p><strong>Progress:</strong> ${Math.round(batch.progress)}%</p>
                    <p><strong>Tasks:</strong> ${batch.completed_tasks}/${batch.total_tasks} completed, ${batch.failed_tasks} failed</p>
                </div>
                <div class="progress-bar">
                    <div class="progress" style="width: ${batch.progress}%"></div>
                </div>
                <button class="view-tasks" data-batch-id="${batch.batch_id}">View Tasks</button>
            `;
            
            container.appendChild(card);
            
            // Add event listener to view tasks button
            card.querySelector('.view-tasks').addEventListener('click', () => {
                window.location.href = `/history-ui?batch=${batch.batch_id}`;
            });
        });
    } catch (error) {
        console.error('Error refreshing batch list:', error);
        alert('Error refreshing batch list: ' + error.message);
    }
}
