// /srv/chatbot/web/app.js

const chatContainer = document.getElementById('chatContainer');
const queryInput = document.getElementById('q');
const submitButton = document.getElementById('go');
const themeSelect = document.getElementById('theme');

// Theme-specific text; the look itself is CSS under [data-theme="..."] in index.html
const THEMES = {
    retro: {
        title: '// CHATBOT INTERFACE',
        subtitle: "Ask any question about Kevin's employment history. Press Enter or click SEND to submit.",
        avatar: '',
        labels: { user: '> USER', bot: '> SYSTEM' },
        send: 'SEND',
        sending: 'SENDING...',
        typing: 'Processing',
        placeholder: 'Type your message...',
        empty: 'No messages yet. Start a conversation...',
    },
    ios: {
        title: "Kevin's Resume Bot",
        subtitle: "Ask any question about Kevin's employment history.",
        avatar: 'KD',
        labels: { user: 'You', bot: "Kevin's Resume Bot" },
        send: '↑',
        sending: '↑',
        typing: 'Typing',
        placeholder: 'iMessage',
        empty: "iMessage · Today\nAsk any question about Kevin's employment history.",
    },
    android: {
        title: "Kevin's Resume Bot",
        subtitle: "Ask any question about Kevin's employment history.",
        avatar: 'K',
        labels: { user: 'You', bot: "Kevin's Resume Bot" },
        send: 'Send',
        sending: 'Send',
        typing: 'Typing',
        placeholder: 'Text message',
        empty: "Today\nAsk any question about Kevin's employment history.",
    },
    contrast: {
        title: 'Ask About Kevin',
        subtitle: "Ask any question about Kevin's employment history. Press Enter or select Send.",
        avatar: '',
        labels: { user: 'You said:', bot: 'Answer:' },
        send: 'Send',
        sending: 'Sending…',
        typing: 'Working on an answer',
        placeholder: 'Type your question',
        empty: 'No messages yet. Type a question below to start.',
    },
    ironman: {
        title: 'J.A.R.V.I.S.',
        subtitle: "Just A Rather Very Intelligent Resume System. Query Kevin's employment history.",
        avatar: '',
        labels: { user: 'Visitor', bot: 'J.A.R.V.I.S.' },
        send: 'Engage',
        sending: 'Computing',
        typing: 'Analyzing',
        placeholder: 'State your query...',
        empty: 'All systems online. Awaiting your query.',
    },
    american: {
        title: '★ Ask About Kevin ★',
        subtitle: "Ask any question about Kevin's employment history.",
        avatar: '',
        labels: { user: 'You', bot: "Kevin's Resume Bot" },
        send: 'Send ★',
        sending: 'Sending',
        typing: 'Working on it',
        placeholder: 'Ask your question...',
        empty: 'Land of the free, home of the resume. Ask away!',
    },
    canadian: {
        title: 'Ask About Kevin',
        subtitle: "Ask any question about Kevin's employment history.",
        avatar: '🍁',
        labels: { user: 'You', bot: "Kevin's Resume Bot, eh" },
        send: 'Send',
        sending: 'Sorry, one sec',
        typing: 'Just a sec, eh',
        placeholder: 'Ask away, eh?',
        empty: "Welcome, friend! Ask any question about Kevin's employment history.",
    },
    synthwave: {
        title: 'ASK KEVIN',
        subtitle: "Ask any question about Kevin's employment history.",
        avatar: '',
        labels: { user: 'Player 1', bot: 'KEVIN.EXE' },
        send: 'SEND',
        sending: 'LOADING',
        typing: 'Loading',
        placeholder: 'Type your message...',
        empty: 'INSERT QUESTION TO CONTINUE',
    },
    halloween: {
        title: "Kevin's Haunted Resume",
        subtitle: "Ask the spirits about Kevin's employment history... if you dare.",
        avatar: '🎃',
        labels: { user: 'Trick-or-Treater', bot: 'Ghost of Resumes Past' },
        send: 'Boo!',
        sending: 'Summoning',
        typing: 'Consulting the spirits',
        placeholder: 'Ask, if you dare...',
        empty: "🕸️ The spirits are listening.\nAsk anything about Kevin's career.",
    },
    christmas: {
        title: "Kevin's Holiday Resume",
        subtitle: "Ask Santa's helper about Kevin's employment history.",
        avatar: '🎄',
        labels: { user: 'You', bot: "Santa's Helper" },
        send: 'Send',
        sending: 'Wrapping',
        typing: 'Checking the list',
        placeholder: 'Ask your question...',
        empty: "❄️ Ho ho ho!\nAsk anything about Kevin's career.",
    },
};

let currentTheme = THEMES[document.documentElement.dataset.theme] ? document.documentElement.dataset.theme : 'ios';

function theme() {
    return THEMES[currentTheme];
}

function applyTheme(name, save = true) {
    if (!THEMES[name]) {
        name = 'ios';
    }
    currentTheme = name;
    const t = theme();
    document.documentElement.dataset.theme = name;
    themeSelect.value = name;
    document.getElementById('title').textContent = t.title;
    document.getElementById('subtitle').textContent = t.subtitle;
    document.getElementById('avatar').textContent = t.avatar;
    queryInput.placeholder = t.placeholder;
    submitButton.textContent = submitButton.disabled ? t.sending : t.send;
    submitButton.setAttribute('aria-label', 'Send');
    chatContainer.querySelectorAll('.message').forEach(msg => {
        const label = msg.querySelector('.message-label');
        if (label) {
            label.textContent = t.labels[msg.classList.contains('user') ? 'user' : 'bot'];
        }
    });
    chatContainer.querySelectorAll('.typing-text').forEach(el => {
        el.textContent = t.typing;
    });
    const emptyState = chatContainer.querySelector('.empty-state');
    if (emptyState) {
        emptyState.textContent = t.empty;
    }
    if (save) {
        try {
            localStorage.setItem('chatbot-theme', name);
        } catch (e) {
            // Storage unavailable (private mode etc.); the theme still applies for this visit
        }
    }
}

themeSelect.addEventListener('change', () => applyTheme(themeSelect.value));

let typingIndicatorInterval = null;

function addMessage(content, sender, isTyping = false, isError = false) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${sender}${isTyping ? ' typing' : ''}${isError ? ' error' : ''}`;
    
    const label = document.createElement('div');
    label.className = 'message-label';
    label.textContent = theme().labels[sender === 'user' ? 'user' : 'bot'];
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    if (isTyping) {
        const typingText = document.createElement('span');
        typingText.className = 'typing-text';
        typingText.textContent = content;
        const typingSpan = document.createElement('span');
        typingSpan.className = 'typing-indicator';
        typingSpan.textContent = '.';
        // Dots bubble used by the iOS theme; CSS shows one style or the other
        const typingDots = document.createElement('span');
        typingDots.className = 'typing-dots';
        typingDots.setAttribute('aria-label', content);
        for (let i = 0; i < 3; i++) {
            typingDots.appendChild(document.createElement('span'));
        }
        contentDiv.appendChild(typingText);
        contentDiv.appendChild(typingSpan);
        contentDiv.appendChild(typingDots);
        
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
        emptyDiv.textContent = theme().empty;
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
    submitButton.textContent = theme().sending;
    
    // Add typing indicator
    const typingContentDiv = addMessage(theme().typing, 'bot', true);
    
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
        submitButton.textContent = theme().send;
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
                                label.textContent = theme().labels.bot;
                                
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
applyTheme(currentTheme, false);
