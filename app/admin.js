// /srv/chatbot/app/admin.js

const chatContainer = document.getElementById('chatContainer');
const queryInput = document.getElementById('q');
const titleInput = document.getElementById('title');
const sourceInput = document.getElementById('source');
const submitButton = document.getElementById('go');
const apiKeyInput = document.getElementById('apiKey');
const apiKeySection = document.getElementById('apiKeySection');
const saveApiKeyButton = document.getElementById('saveApiKey');
const authStatus = document.getElementById('authStatus');

let typingIndicatorInterval = null;

// API Key management
const API_KEY_STORAGE_KEY = 'admin_api_key';

function getApiKey() {
    return localStorage.getItem(API_KEY_STORAGE_KEY) || '';
}

function setApiKey(key) {
    if (key) {
        localStorage.setItem(API_KEY_STORAGE_KEY, key);
    } else {
        localStorage.removeItem(API_KEY_STORAGE_KEY);
    }
}

function checkApiKey() {
    const apiKey = getApiKey();
    if (!apiKey) {
        apiKeySection.style.display = 'block';
        authStatus.textContent = '⚠️ API key required for authentication';
        authStatus.style.color = '#ff8800';
        return false;
    } else {
        apiKeySection.style.display = 'none';
        authStatus.textContent = '✓ Authenticated';
        authStatus.style.color = '#00ff41';
        return true;
    }
}

// Save API key button handler
saveApiKeyButton.onclick = () => {
    const key = apiKeyInput.value.trim();
    if (!key) {
        alert('Please enter an API key');
        return;
    }
    setApiKey(key);
    apiKeyInput.value = '';
    checkApiKey();
    addMessage('API key saved. You can now ingest content.', 'bot', false, false, true);
};

// Check API key on load
checkApiKey();

function addMessage(content, sender, isTyping = false, isError = false, isSuccess = false) {
    const messageDiv = document.createElement('div');
    let classes = `message ${sender}`;
    if (isTyping) classes += ' typing';
    if (isError) classes += ' error';
    if (isSuccess) classes += ' success';
    messageDiv.className = classes;
    
    const label = document.createElement('div');
    label.className = 'message-label';
    if (sender === 'user') {
        label.textContent = '> ADMIN';
    } else if (isSuccess) {
        label.textContent = '> SUCCESS';
    } else {
        label.textContent = '> SYSTEM';
    }
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    if (isTyping) {
        const typingSpan = document.createElement('span');
        typingSpan.className = 'typing-indicator';
        typingSpan.textContent = '.';
        contentDiv.textContent = content;
        contentDiv.appendChild(typingSpan);
        
        // Animate typing indicator
        let dotCount = 0;
        typingIndicatorInterval = setInterval(() => {
            dotCount = (dotCount + 1) % 3;
            typingSpan.textContent = '.'.repeat(dotCount + 1);
        }, 500);
        
        // Store interval ID on the message div for cleanup
        messageDiv._typingInterval = typingIndicatorInterval;
    } else {
        contentDiv.textContent = content;
    }
    
    messageDiv.appendChild(label);
    messageDiv.appendChild(contentDiv);
    chatContainer.appendChild(messageDiv);
    
    // Scroll to bottom
    chatContainer.scrollTop = chatContainer.scrollHeight;
    
    return contentDiv;
}

function removeTypingIndicator() {
    const typingMessages = chatContainer.querySelectorAll('.message.typing');
    typingMessages.forEach(msg => {
        // Clear any associated interval
        if (msg._typingInterval) {
            clearInterval(msg._typingInterval);
        }
        msg.remove();
    });
    // Also clear the global interval if it exists
    if (typingIndicatorInterval) {
        clearInterval(typingIndicatorInterval);
        typingIndicatorInterval = null;
    }
}

function showEmptyState() {
    if (chatContainer.children.length === 0) {
        const emptyDiv = document.createElement('div');
        emptyDiv.className = 'empty-state';
        emptyDiv.textContent = 'No ingestion yet. Enter content to add to the knowledge base...';
        chatContainer.appendChild(emptyDiv);
    }
}

function removeEmptyState() {
    const emptyState = chatContainer.querySelector('.empty-state');
    if (emptyState) {
        emptyState.remove();
    }
}

function formatIngestionResponse(data) {
    let message = `✅ Successfully ingested ${data.chunks} chunk${data.chunks !== 1 ? 's' : ''}\n\n`;
    message += `Title: ${data.title}\n`;
    message += `Source: ${data.source}\n`;
    message += `Document ID: ${data.doc_id}`;
    return message;
}

document.getElementById('go').onclick = async () => {
    const content = queryInput.value.trim();
    
    if (!content) {
        return;
    }
    
    // Check API key
    const apiKey = getApiKey();
    if (!apiKey) {
        alert('Please enter and save your API key first');
        apiKeySection.style.display = 'block';
        apiKeyInput.focus();
        return;
    }
    
    // Remove empty state if present
    removeEmptyState();
    
    // Add user message (preview)
    const preview = content.length > 200 ? content.substring(0, 200) + '...' : content;
    addMessage(preview, 'user');
    
    // Clear inputs
    const title = titleInput.value.trim() || undefined;
    const source = sourceInput.value.trim() || undefined;
    queryInput.value = '';
    titleInput.value = '';
    sourceInput.value = '';
    
    // Disable button
    submitButton.disabled = true;
    submitButton.textContent = 'INGESTING...';
    
    // Add typing indicator
    const typingContentDiv = addMessage('Processing and ingesting content', 'bot', true);
    
    try {
        const payload = {
            content: content,
        };
        if (title) payload.title = title;
        if (source) payload.source = source;
        
        const res = await fetch('/chatbot/api/admin/ingest', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-API-Key': apiKey
            },
            body: JSON.stringify(payload)
        });
        
        if (!res.ok) {
            // Try to get error message
            const errorText = await res.text();
            let errorData;
            try {
                errorData = JSON.parse(errorText);
            } catch {
                errorData = { detail: `HTTP ${res.status}: ${res.statusText}` };
            }
            
            // Handle authentication errors
            if (res.status === 401) {
                // Clear invalid API key
                setApiKey('');
                checkApiKey();
                throw new Error('Authentication failed. Please check your API key and try again.');
            }
            
            throw new Error(errorData.detail || `Request failed with status ${res.status}`);
        }
        
        const data = await res.json();
        removeTypingIndicator();
        
        if (data.status === 'success') {
            addMessage(formatIngestionResponse(data), 'bot', false, false, true);
        } else {
            throw new Error(data.message || 'Ingestion failed');
        }
    } catch (error) {
        console.error('Ingestion request failed:', error);
        removeTypingIndicator();
        addMessage(`Error: ${error.message || 'Failed to ingest content. Please try again.'}`, 'bot', false, true);
    } finally {
        submitButton.disabled = false;
        submitButton.textContent = 'INGEST';
        queryInput.focus();
    }
};

// Allow Ctrl+Enter or Cmd+Enter to submit
queryInput.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !submitButton.disabled) {
        e.preventDefault();
        submitButton.click();
    }
});

// Show empty state on load
showEmptyState();
