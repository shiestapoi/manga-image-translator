// Main application JavaScript code
document.addEventListener('DOMContentLoaded', function() {
  // Initialize dark mode based on user preference or system setting
  initializeDarkMode();
});

// Dark Mode Management
function initializeDarkMode() {
  // Check for saved preference or system preference
  const savedMode = localStorage.getItem('dark-mode');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  
  // Set initial mode - saved preference takes priority
  if (savedMode === 'true' || (savedMode === null && prefersDark)) {
    document.documentElement.classList.add('dark');
  } else {
    document.documentElement.classList.remove('dark');
  }
}

function toggleDarkMode() {
  const isDarkMode = document.documentElement.classList.contains('dark');
  
  if (isDarkMode) {
    document.documentElement.classList.remove('dark');
    localStorage.setItem('dark-mode', 'false');
  } else {
    document.documentElement.classList.add('dark');
    localStorage.setItem('dark-mode', 'true');
  }
}

// Format utilities
function formatSize(bytes) {
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  if (bytes === 0) return "0B";
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / k ** i).toFixed(2)}${sizes[i]}`;
}

function formatProgress(loaded, total) {
  return `${formatSize(loaded)}/${formatSize(total)}`;
}

function formatDate(dateString) {
  const date = new Date(dateString);
  return date.toLocaleString();
}

// File handling helpers
function getFilenameFromPath(path) {
  if (!path) return "";
  return path.split("/").pop();
}

function isValidImageType(fileType) {
  const acceptTypes = [
    "image/png",
    "image/jpeg",
    "image/bmp",
    "image/webp",
  ];
  return acceptTypes.includes(fileType);
}

// PetiteVue Application Configuration - setup all functionality
const createApp = () => {
  return {
    // State management
    isDarkMode: document.documentElement.classList.contains('dark'),
    activeTab: "single",
    
    // Single image upload
    file: null,
    fileUri: null,
    
    // Translation status
    status: null,
    progress: null,
    progressPercent: null,
    queuePos: null,
    errorDetails: null,
    error: false,
    result: null,
    resultUri: null,
    
    // Configuration options
    detectionResolution: "1536",
    textDetector: "default",
    renderTextDirection: "auto",
    translator: "deepl",
    targetLanguage: "ENG",
    inpaintingSize: "2048",
    customUnclipRatio: 2.3,
    customBoxThreshold: 0.7,
    maskDilationOffset: 30,
    inpainter: "default",
    
    validTranslators: [
      "youdao", "baidu", "deepl", "papago", "caiyun", "sakura", 
      "offline", "openai", "deepseek", "groq", "gemini", 
      "custom_openai", "nllb", "nllb_big", "sugoi", "jparacrawl", 
      "jparacrawl_big", "m2m100", "m2m100_big", "mbart50", 
      "qwen2", "qwen2_big", "none", "original"
    ],
    
    // Multiple image upload
    multiFiles: [],
    batchName: "",
    processingBatch: false,
    batchResult: null,
    
    // Queue status
    queueStatus: null,
    activeBatches: null,
    
    // History
    historyItems: null,
    historyBatches: [],
    historyBatchFilter: "",
    historyStatusFilter: "",
    
    // Modal
    showResultModal: false,
    modalImageUrl: "",
    currentModalItem: null,
    
    // MangaDex downloader
    mangadexChapterId: "",
    mangadexProcessing: false,
    mangadexStatus: "",
    mangadexError: false,
    
    // Lifecycle methods
    onMounted() {
      // Listen for paste events
      window.addEventListener("paste", this.onPaste);
      
      // Load queue status on mount
      this.refreshQueueStatus();
      
      // Load history data if on history tab
      if (this.activeTab === "history") {
        this.refreshHistory();
      }
    },
    
    // Computed properties
    get statusText() {
      switch (this.status) {
        case "upload": 
          return this.progress ? `Uploading (${this.progress})` : "Uploading";
        case "pending": 
          return this.queuePos ? `Queuing, your position is ${this.queuePos}` : "Processing";
        case "detection": 
          return "Detecting texts";
        case "ocr": 
          return "Running OCR";
        case "mask-generation": 
          return "Generating text mask";
        case "inpainting": 
          return "Running inpainting";
        case "upscaling": 
          return "Running upscaling";
        case "translating": 
          return "Translating";
        case "rendering": 
          return "Rendering translated texts";
        case "finished": 
          return "Downloading image";
        case "error": 
          return "Something went wrong, please try again";
        case "error-upload": 
          return "Upload failed, please try again";
        case "error-lang": 
          return "Your target language is not supported by the chosen translator";
        case "error-translating": 
          return "Did not get any text back from the text translation service";
        case "error-too-large": 
          return "Image size too large (greater than 8000x8000 px)";
        case "error-disconnect": 
          return "Lost connection to server.";
        default: 
          return this.status;
      }
    },
    
    get filteredHistoryItems() {
      if (!this.historyItems) return [];
      
      return this.historyItems.filter((item) => {
        // Filter by batch if specified
        if (this.historyBatchFilter && item.batch_id !== this.historyBatchFilter) {
          return false;
        }
        
        // Filter by status if specified
        if (this.historyStatusFilter && item.status !== this.historyStatusFilter) {
          return false;
        }
        
        return true;
      });
    },
    
    // Event handlers
    toggleDarkMode() {
      this.isDarkMode = !this.isDarkMode;
      
      if (this.isDarkMode) {
        document.documentElement.classList.add('dark');
        localStorage.setItem('dark-mode', 'true');
      } else {
        document.documentElement.classList.remove('dark');
        localStorage.setItem('dark-mode', 'false');
      }
    },
    
    switchTab(tab) {
      this.activeTab = tab;
      
      // Load data based on active tab
      if (tab === "queue") {
        this.refreshQueueStatus();
      } else if (tab === "history") {
        this.refreshHistory();
      }
    },
    
    // File handling
    onDrop(e) {
      const file = e.dataTransfer?.files?.[0];
      if (file && isValidImageType(file.type)) {
        this.file = file;
        this.fileUri = URL.createObjectURL(file);
      }
    },
    
    onFileChange(e) {
      const file = e.target.files?.[0];
      if (file && isValidImageType(file.type)) {
        this.file = file;
        this.fileUri = URL.createObjectURL(file);
      }
    },
    
    onPaste(e) {
      const items = (e.clipboardData || e.originalEvent.clipboardData).items;
      for (const item of items) {
        if (item.kind === "file") {
          const file = item.getAsFile();
          if (!file || !isValidImageType(file.type)) continue;
          this.file = file;
          this.fileUri = URL.createObjectURL(file);
        }
      }
    },
    
    // Multiple image upload handlers
    triggerMultiFileUpload() {
      document.getElementById("multi-file-input").click();
    },
    
    onMultiDrop(e) {
      const files = e.dataTransfer?.files;
      if (files && files.length > 0) {
        this.addMultiFiles(files);
      }
    },
    
    onMultiFileChange(e) {
      const files = e.target.files;
      if (files && files.length > 0) {
        this.addMultiFiles(files);
      }
    },
    
    addMultiFiles(files) {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        if (isValidImageType(file.type)) {
          // Check if file already exists in the list
          const exists = this.multiFiles.some(
            (f) => f.name === file.name && f.size === file.size
          );
          
          if (!exists) {
            this.multiFiles.push(file);
          }
        }
      }
    },
    
    getFilePreviewUrl(file) {
      return URL.createObjectURL(file);
    },
    
    removeFile(index) {
      this.multiFiles.splice(index, 1);
    },
    
    clearMultiFiles() {
      this.multiFiles = [];
      this.batchResult = null;
    },
    
    getTranslatorName(key) {
      if (key === "none") return "No Text";
      return key ? key[0].toUpperCase() + key.slice(1) : "";
    },
    
    // Translation process
    clear() {
      this.file = null;
      this.fileUri = null;
      this.result = null;
      this.resultUri = null;
      this.status = null;
      this.errorDetails = null;
      this.progressPercent = null;
    },
    
    downloadImage() {
      if (!this.result) return;
      
      const link = document.createElement("a");
      link.href = this.resultUri;
      const originalFilename = this.file?.name || "image";
      const filenameWithoutExt = originalFilename.substring(0, originalFilename.lastIndexOf(".")) || originalFilename;
      link.download = `translated-${filenameWithoutExt}.png`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    },
    
    // API communication methods
    async processMultiFiles() {
      if (this.processingBatch || this.multiFiles.length === 0) return;
      
      this.processingBatch = true;
      
      try {
        // Step 1: Create batch
        const batchName = this.batchName || `Batch ${new Date().toLocaleString()}`;
        const batchResponse = await fetch("/batch/create", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            name: batchName,
            description: `Created on ${new Date().toLocaleString()}`,
          }),
        });
        
        if (!batchResponse.ok) {
          throw new Error("Failed to create batch");
        }
        
        const batchData = await batchResponse.json();
        const batchId = batchData.batch_id;
        
        // Step 2: Add files to batch
        const formData = new FormData();
        for (let i = 0; i < this.multiFiles.length; i++) {
          formData.append("images", this.multiFiles[i]);
        }
        
        // Add translation config
        const config = {
          detector: {
            detector: this.textDetector,
            detection_size: parseInt(this.detectionResolution),
            box_threshold: parseFloat(this.customBoxThreshold),
            unclip_ratio: parseFloat(this.customUnclipRatio),
          },
          render: {
            direction: this.renderTextDirection,
          },
          translator: {
            translator: this.translator,
            target_lang: this.targetLanguage,
          },
          inpainter: {
            inpainter: this.inpainter,
            inpainting_size: parseInt(this.inpaintingSize),
          },
          mask_dilation_offset: parseInt(this.maskDilationOffset),
        };
        
        formData.append("config", JSON.stringify(config));
        
        const uploadResponse = await fetch(`/batch/${batchId}/add-multiple`, {
          method: "POST",
          body: formData,
        });
        
        if (!uploadResponse.ok) {
          throw new Error("Failed to add images to batch");
        }
        
        const tasksData = await uploadResponse.json();
        
        // Step 3: Show result
        this.batchResult = {
          ...batchData,
          total_tasks: tasksData.length,
        };
        
        // Refresh queue status
        this.refreshQueueStatus();
      } catch (error) {
        console.error("Error processing files:", error);
        alert("Error: " + error.message);
      } finally {
        this.processingBatch = false;
      }
    },
    
    async refreshQueueStatus() {
      try {
        // Get queue status
        const statusResponse = await fetch("/queue/status");
        if (statusResponse.ok) {
          this.queueStatus = await statusResponse.json();
        }
        
        // Get active batches
        const batchesResponse = await fetch("/batch");
        if (batchesResponse.ok) {
          const batches = await batchesResponse.json();
          // Filter only active batches (not completed)
          this.activeBatches = batches.filter(
            (b) => b.status === "queued" || b.status === "created" || b.status === "in_progress"
          );
        }
      } catch (error) {
        console.error("Error refreshing queue status:", error);
      }
    },
    
    viewBatchDetails(batchId) {
      this.historyBatchFilter = batchId;
      this.switchTab("history");
    },
    
    async refreshHistory() {
      try {
        // Get history items
        const historyResponse = await fetch("/history");
        if (historyResponse.ok) {
          this.historyItems = await historyResponse.json();
        }
        
        // Get batches for filter
        const batchesResponse = await fetch("/batch");
        if (batchesResponse.ok) {
          this.historyBatches = await batchesResponse.json();
        }
      } catch (error) {
        console.error("Error refreshing history:", error);
      }
    },
    
    getResultImageUrl(item) {
      // If result_path is a full URL (R2), use as is
      if (item.result_path && item.result_path.startsWith("http")) {
        return item.result_path;
      }
      const path = "/static/results/";
      return item.result_path ? `${path}${item.result_path}` : "";
    },
    
    viewHistoryResult(item) {
      if (item.result_path) {
        if (item.result_path && item.result_path.startsWith("http")) {
          this.modalImageUrl = item.result_path;
        } else {
          const date = new Date(item.created_at);
          const formattedDate = `${date.getFullYear()}${(date.getMonth() + 1)
            .toString()
            .padStart(2, "0")}${date.getDate().toString().padStart(2, "0")}`;
          this.modalImageUrl = `/static/results/${formattedDate}/${getFilenameFromPath(
            item.result_path
          )}`;
        }
        this.currentModalItem = item;
        this.showResultModal = true;
      }
    },
    
    closeResultModal() {
      this.showResultModal = false;
      this.modalImageUrl = "";
      this.currentModalItem = null;
    },
    
    downloadModalImage() {
      if (!this.modalImageUrl) return;
      
      const link = document.createElement("a");
      link.href = this.modalImageUrl;
      link.download = `translated-${getFilenameFromPath(
        this.currentModalItem?.result_path || "image.png"
      )}`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    },
    
    async processMangadexChapterAuto() {
      if (!this.mangadexChapterId || this.mangadexProcessing) return;
      
      this.mangadexProcessing = true;
      this.mangadexStatus = "Fetching chapter information...";
      this.mangadexError = false;
      
      try {
        // Create form data with translation settings
        const formData = new FormData();
        
        // Add translation config
        const config = {
          detector: {
            detector: this.textDetector,
            detection_size: parseInt(this.detectionResolution),
            box_threshold: parseFloat(this.customBoxThreshold),
            unclip_ratio: parseFloat(this.customUnclipRatio),
          },
          render: {
            direction: this.renderTextDirection,
          },
          translator: {
            translator: this.translator,
            target_lang: this.targetLanguage,
          },
          inpainter: {
            inpainter: this.inpainter,
            inpainting_size: parseInt(this.inpaintingSize),
          },
          mask_dilation_offset: parseInt(this.maskDilationOffset),
        };
        
        formData.append("config", JSON.stringify(config));
        
        // Call the automated process endpoint
        this.mangadexStatus = "Downloading chapter and creating translation batch...";
        const response = await fetch(`/mangadex/process/chapter/${this.mangadexChapterId}`, {
          method: "POST",
          body: formData
        });
        
        if (!response.ok) {
          const errorText = await response.text();
          throw new Error(`Failed to process chapter: ${errorText}`);
        }
        
        const batchData = await response.json();
        
        this.batchResult = batchData;
        this.mangadexStatus = `Successfully processed! Created batch "${batchData.name}" with ${batchData.total_tasks} images. You can track translation progress in the Queue Status tab.`;
        
        // Refresh queue status
        this.refreshQueueStatus();
        
      } catch (error) {
        console.error("MangaDex processing error:", error);
        this.mangadexStatus = `Error: ${error.message}`;
        this.mangadexError = true;
      } finally {
        this.mangadexProcessing = false;
      }
    },
    
    async startTranslator() {
      try {
        const response = await fetch("/start-translator", {
          method: "POST",
        });
        if (response.ok) {
          const data = await response.json();
          alert(data.message);
        } else {
          alert("Failed to start translator client process. Check logs for details.");
        }
      } catch (error) {
        console.error("Error starting translator client process:", error);
        alert("An error occurred while trying to start the translator client process.");
      }
    },
    
    async submitTranslation() {
      if (!this.file) return;
      
      this.progress = null;
      this.progressPercent = null;
      this.queuePos = null;
      this.status = "upload";
      this.errorDetails = null;
      this.error = false;
      let buffer = new Uint8Array();
      
      const formData = new FormData();
      formData.append("image", this.file);
      
      const config = {
        detector: {
          detector: this.textDetector,
          detection_size: parseInt(this.detectionResolution),
          box_threshold: parseFloat(this.customBoxThreshold),
          unclip_ratio: parseFloat(this.customUnclipRatio)
        },
        render: {
          direction: this.renderTextDirection
        },
        translator: {
          translator: this.translator,
          target_lang: this.targetLanguage
        },
        inpainter: {
          inpainter: this.inpainter,
          inpainting_size: parseInt(this.inpaintingSize)
        },
        mask_dilation_offset: parseInt(this.maskDilationOffset)
      };
      
      formData.append("config", JSON.stringify(config));
      
      try {
        const response = await fetch("/translate/with-form/image/stream", {
          method: "POST",
          body: formData,
        });
        
        if (response.status !== 200) {
          this.status = "error-upload";
          this.error = true;
          return;
        }
        
        const reader = response.body.getReader();
        
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          
          // Process the chunk
          const newBuffer = new Uint8Array(buffer.length + value.length);
          newBuffer.set(buffer);
          newBuffer.set(value, buffer.length);
          buffer = newBuffer;
          
          while (buffer.length >= 5) {
            const dataSize = new DataView(buffer.buffer).getUint32(1, false);
            const totalSize = 5 + dataSize;
            if (buffer.length < totalSize) {
              break;
            }
            
            const statusCode = buffer[0];
            const decoder = new TextDecoder("utf-8");
            const data = buffer.slice(5, totalSize);
            
            switch (statusCode) {
              case 0:
                const resultBlob = new Blob([data], { type: "image/png" });
                this.result = resultBlob;
                this.resultUri = URL.createObjectURL(resultBlob);
                this.status = null;
                // Refresh history after successful translation
                setTimeout(() => this.refreshHistory(), 1000);
                break;
              case 1:
                const statusMsg = decoder.decode(data);
                this.status = statusMsg;
                
                // Set progress percentage for specific steps
                if (statusMsg === "detection") {
                  this.progressPercent = 20;
                } else if (statusMsg === "ocr") {
                  this.progressPercent = 30;
                } else if (statusMsg === "mask-generation") {
                  this.progressPercent = 40;
                } else if (statusMsg === "inpainting") {
                  this.progressPercent = 60;
                } else if (statusMsg === "translating") {
                  this.progressPercent = 75;
                } else if (statusMsg === "rendering") {
                  this.progressPercent = 90;
                } else if (statusMsg === "finished") {
                  this.progressPercent = 100;
                }
                break;
              case 2:
                this.status = "error";
                this.error = true;
                this.errorDetails = decoder.decode(data);
                console.error(this.errorDetails);
                break;
              case 3:
                this.status = "pending";
                this.queuePos = decoder.decode(data);
                break;
              case 4:
                this.status = "pending";
                this.queuePos = null;
                this.progressPercent = 10;
                break;
            }
            
            buffer = buffer.slice(totalSize);
          }
        }
      } catch (error) {
        console.error(error);
        this.status = "error-disconnect";
        this.error = true;
        this.errorDetails = "Check your network connection.";
      }
    }
  };
};

// Make the PetiteVue app available globally
window.createTranslationApp = createApp;