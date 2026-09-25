// /srv/chatbot/web/app.js

const chatContainer = document.getElementById('chatContainer');
const queryInput = document.getElementById('q');
const submitButton = document.getElementById('go');

let typingIndicatorInterval = null;

function addMessage(content, sender, isTyping = false, isError = false) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${sender}${isTyping ? ' typing' : ''}${isError ? ' error' : ''}`;
    
    const label = document.createElement('div');
    label.className = 'message-label';
    label.textContent = sender === 'user' ? '> USER' : '> SYSTEM';
    
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
        emptyDiv.textContent = 'No messages yet. Start a conversation...';
        chatContainer.appendChild(emptyDiv);
    }
}

function removeEmptyState() {
    const emptyState = chatContainer.querySelector('.empty-state');
    if (emptyState) {
        emptyState.remove();
    }
}

document.getElementById('go').onclick = async () => {
    const query = queryInput.value.trim();
    
    if (!query) {
        return;
    }
    
    // Remove empty state if present
    removeEmptyState();
    
    // Add user message
    addMessage(query, 'user');
    
    // Clear input
    queryInput.value = '';
    
    // Disable button
    submitButton.disabled = true;
    submitButton.textContent = 'SENDING...';
    
    // Add typing indicator
    const typingContentDiv = addMessage('Processing', 'bot', true);
    
    try {
        // Use streaming by default
        const res = await fetch('/chatbot/api/chat?stream=true', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({query: query})
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
            throw new Error(errorData.detail || `Request failed with status ${res.status}`);
        }
        
        // Check if response is streaming
        const contentType = res.headers.get('content-type') || '';
        if (contentType.includes('text/event-stream')) {
            // Handle streaming response - typing indicator will be removed when first token arrives
            await handleStreamingResponse(res);
        } else {
            // Fallback to non-streaming
            removeTypingIndicator();
            const data = await res.json();
            if (data.answer) {
                addMessage(data.answer, 'bot');
            } else {
                throw new Error('No answer received from server');
            }
        }
    } catch (error) {
        console.error('Chat request failed:', error);
        removeTypingIndicator();
        addMessage(`Error: ${error.message || 'Failed to get response. Please try again.'}`, 'bot', false, true);
    } finally {
        submitButton.disabled = false;
        submitButton.textContent = 'SEND';
        queryInput.focus();
    }
}

async function handleStreamingResponse(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullAnswer = '';
    let contentDiv = null;
    
    try {
        while (true) {
            const { done, value } = await reader.read();
            
            if (done) {
                break;
            }
            
            // Decode chunk and add to buffer
            buffer += decoder.decode(value, { stream: true });
            
            // Process complete lines (SSE format: "data: {...}\n\n")
            const lines = buffer.split('\n');
            buffer = lines.pop() || ''; // Keep incomplete line in buffer
            
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const jsonStr = line.slice(6); // Remove "data: " prefix
                        const data = JSON.parse(jsonStr);
                        
                        if (data.error) {
                            throw new Error(data.error);
                        }
                        
                        if (data.token) {
                            if (!contentDiv) {
                                // Remove typing indicator now that we have the first token
                                removeTypingIndicator();
                                
                                // Create bot message container
                                const messageDiv = document.createElement('div');
                                messageDiv.className = 'message bot';
                                
                                const label = document.createElement('div');
                                label.className = 'message-label';
                                label.textContent = '> SYSTEM';
                                
                                contentDiv = document.createElement('div');
                                contentDiv.className = 'message-content';
                                
                                messageDiv.appendChild(label);
                                messageDiv.appendChild(contentDiv);
                                chatContainer.appendChild(messageDiv);
                            }
                            
                            fullAnswer += data.token;
                            contentDiv.textContent = fullAnswer;
                            
                            // Auto-scroll to bottom
                            chatContainer.scrollTop = chatContainer.scrollHeight;
                        }
                        
                        if (data.done) {
                            return;
                        }
                    } catch (e) {
                        if (e instanceof SyntaxError) {
                            // Invalid JSON, skip this line
                            continue;
                        }
                        throw e;
                    }
                }
            }
        }
        
        // Finalize - ensure we have a message
        if (fullAnswer && !contentDiv) {
            removeTypingIndicator();
            addMessage(fullAnswer, 'bot');
        } else if (!contentDiv) {
            // No tokens received, remove typing indicator anyway
            removeTypingIndicator();
        }
    } catch (error) {
        console.error('Streaming error:', error);
        if (contentDiv) {
            contentDiv.parentElement.classList.add('error');
            contentDiv.textContent = `Error: ${error.message || 'Failed to stream response'}`;
        } else {
            addMessage(`Error: ${error.message || 'Failed to stream response'}`, 'bot', false, true);
        }
        throw error;
    }
}

// Allow Enter key to submit
queryInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter' && !submitButton.disabled) {
        submitButton.click();
    }
});

// Show empty state on load
showEmptyState();
