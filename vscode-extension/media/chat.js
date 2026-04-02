// @ts-nocheck
(function () {
  const vscode = acquireVsCodeApi();
  const messagesEl = document.getElementById("messages");
  const statusPill = document.getElementById("status-pill");
  const input = document.getElementById("input");

  // Persistent elements that get updated in-place
  let todoBlockEl = null;
  let spinnerEl = null;

  // ===== Chat history persistence =====
  // Messages are stored in vscode state so they survive panel close/reopen.
  const savedState = vscode.getState() || {};
  let chatHistory = savedState.chatHistory || [];
  const sessionId = document.body.getAttribute("data-session-id") || savedState.sessionId || "";

  // Save state immediately so the serializer has the sessionId on reload
  if (sessionId) {
    vscode.setState({ chatHistory, sessionId });
  }

  function saveHistory() {
    vscode.setState({ chatHistory, sessionId });
    // Also notify the extension so it persists to workspace storage (sessions)
    vscode.postMessage({ type: "save_history", messages: chatHistory });
  }

  function recordMessage(msg) {
    // Don't persist transient messages — they crash on replay
    if (msg.type === "spinner" || msg.type === "status" || msg.type === "confirm_tool"
        || msg.type === "text_delta" || msg.type === "context_usage") return;
    chatHistory.push(msg);
    // Cap history to prevent unbounded growth
    if (chatHistory.length > 500) {
      chatHistory = chatHistory.slice(-400);
    }
    saveHistory();
  }

  // Replay saved history on load (from webview state)
  // Skip transient message types that can't be replayed properly
  const SKIP_ON_REPLAY = new Set(["text_delta", "context_usage", "spinner", "status", "confirm_tool"]);

  /** Migrate old history: merge text_delta runs into agent messages, skip transient types */
  function migrateHistory(msgs) {
    const out = [];
    let deltaBuffer = "";
    for (const msg of msgs) {
      if (!msg) continue;
      if (msg.type === "text_delta") {
        if (msg.text) deltaBuffer += msg.text;
        continue;
      }
      if (deltaBuffer.trim()) {
        out.push({ type: "agent", content: deltaBuffer.trim() });
        deltaBuffer = "";
      }
      if (SKIP_ON_REPLAY.has(msg.type)) continue;
      out.push(msg);
    }
    if (deltaBuffer.trim()) {
      out.push({ type: "agent", content: deltaBuffer.trim() });
    }
    return out;
  }

  function replayHistory() {
    chatHistory = migrateHistory(chatHistory);
    vscode.setState({ chatHistory, sessionId });

    for (let i = 0; i < chatHistory.length; i++) {
      try {
        handleMessage(chatHistory[i], /* skipSave */ true);
      } catch (e) {
        console.warn("[zoomac] replay crash at msg", i, chatHistory[i]?.type, e);
      }
    }
  }

  // ===== Message handling =====

  function handleMessage(data, skipSave) {
    if (!skipSave) {
      recordMessage(data);
    }

    switch (data.type) {
      case "user":
        finalizeLiveText();
        clearSpinner();
        renderUserMessage(data.content);
        break;
      case "agent":
        clearSpinner();
        try { renderAgentText(data.content); } catch(e) { console.warn("[zoomac] renderAgentText error:", e); }
        break;
      case "tool_call":
        finalizeLiveText();
        clearSpinner();
        if (data.data) {
          try { renderToolCall(data.data); } catch(e) { console.warn("[zoomac] renderToolCall error:", e); }
        }
        break;
      case "todo_update":
        renderTodoUpdate(data.todos);
        break;
      case "spinner":
        if (data.active) {
          showSpinner(data.text);
        } else {
          clearSpinner();
        }
        break;
      case "sub_agent":
        clearSpinner();
        renderSubAgent(data.data);
        break;
      case "text_delta":
        appendTextDelta(data.text);
        break;
      case "confirm_tool":
        clearSpinner();
        renderConfirmation(data);
        break;
      case "context_usage":
        updateContextPie(data.used, data.max, data.percent);
        break;
      case "restore_session":
        // Restore — migrate text_deltas into agent messages
        chatHistory = migrateHistory(data.messages || []);
        vscode.setState({ chatHistory, sessionId });
        messagesEl.innerHTML = "";
        todoBlockEl = null;
        spinnerEl = null;
        liveTextEl = null;
        liveTextContent = "";
        for (let ri = 0; ri < chatHistory.length; ri++) {
          try {
            handleMessage(chatHistory[ri], /* skipSave */ true);
          } catch (e) {
            console.warn("[zoomac] restore crash at msg", ri, chatHistory[ri]?.type, e);
          }
        }
        break;
      case "update_title":
        const titleEl = document.getElementById("session-title");
        if (titleEl) titleEl.textContent = data.title || "Session";
        break;
      case "error":
        clearSpinner();
        renderError(data.content);
        break;
      case "status":
        updateStatus(data.content);
        break;
    }
  }

  window.addEventListener("message", (event) => {
    handleMessage(event.data, false);
  });

  // Restore previous conversation from webview state
  replayHistory();

  // Remove loading hint
  const loadingHint = document.getElementById("loading-hint");
  if (loadingHint) loadingHint.remove();

  // Signal to extension that webview is ready to receive messages
  vscode.postMessage({ type: "webview_ready" });

  // ===== Input handling =====

  /** Pending images attached to the next message */
  let pendingImages = [];

  function sendCurrentInput() {
    const text = input.value.trim();
    if (!text && pendingImages.length === 0) return;

    if (pendingImages.length > 0) {
      const images = pendingImages.map((img) => ({
        mediaType: img.mediaType,
        base64: img.base64,
      }));
      vscode.postMessage({
        type: "send_with_images",
        content: text,
        images: images,
      });
      renderUserMessageWithImages(text, pendingImages.map((i) => i.dataUrl));
      pendingImages = [];
      renderImagePreview();
    } else {
      vscode.postMessage({ type: "send", content: text });
    }

    // Show spinner immediately so user sees activity
    showSpinner("Thinking...");

    input.value = "";
    autoResizeInput();
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendCurrentInput();
    }
  });

  input.addEventListener("input", autoResizeInput);

  // ===== Image paste & drop =====

  input.addEventListener("paste", (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;

    for (const item of items) {
      if (item.type.startsWith("image/")) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) addImageFile(file);
        return;
      }
    }
  });

  // Drop zone on the entire body
  document.body.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    document.body.classList.add("drag-over");
  });

  document.body.addEventListener("dragleave", () => {
    document.body.classList.remove("drag-over");
  });

  document.body.addEventListener("drop", (e) => {
    e.preventDefault();
    document.body.classList.remove("drag-over");
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files) return;
    for (const file of files) {
      if (file.type.startsWith("image/")) {
        addImageFile(file);
      }
    }
  });

  function addImageFile(file) {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result;
      // Extract base64 and media type
      const match = dataUrl.match(/^data:(image\/[^;]+);base64,(.+)$/);
      if (!match) return;

      const mediaType = match[1];
      const base64 = match[2];

      pendingImages.push({ mediaType, base64, dataUrl });
      renderImagePreview();
    };
    reader.readAsDataURL(file);
  }

  function renderImagePreview() {
    let previewArea = document.getElementById("image-preview");
    if (!previewArea) {
      previewArea = document.createElement("div");
      previewArea.id = "image-preview";
      previewArea.className = "image-preview-area";
      // Insert before the input row
      const inputArea = document.getElementById("input-area");
      const inputRow = document.getElementById("input-row");
      inputArea.insertBefore(previewArea, inputRow);
    }

    previewArea.innerHTML = "";

    pendingImages.forEach((img, idx) => {
      const wrapper = document.createElement("div");
      wrapper.className = "image-preview-item";

      const imgEl = document.createElement("img");
      imgEl.src = img.dataUrl;

      const removeBtn = document.createElement("button");
      removeBtn.className = "image-preview-remove";
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", () => {
        pendingImages.splice(idx, 1);
        renderImagePreview();
      });

      wrapper.appendChild(imgEl);
      wrapper.appendChild(removeBtn);
      previewArea.appendChild(wrapper);
    });

    if (pendingImages.length === 0 && previewArea) {
      previewArea.remove();
    }
  }

  function renderUserMessageWithImages(text, dataUrls) {
    const wrapper = document.createElement("div");
    wrapper.className = "msg-user";

    const label = document.createElement("div");
    label.className = "msg-user-label";
    label.textContent = "You";

    wrapper.appendChild(label);

    // Image thumbnails
    const imgRow = document.createElement("div");
    imgRow.className = "msg-image-row";
    dataUrls.forEach((url) => {
      const img = document.createElement("img");
      img.src = url;
      img.className = "msg-image-thumb";
      imgRow.appendChild(img);
    });
    wrapper.appendChild(imgRow);

    if (text) {
      const body = document.createElement("div");
      body.textContent = text;
      wrapper.appendChild(body);
    }

    messagesEl.appendChild(wrapper);
    scrollToBottom();
  }

  // ===== Auto-edit toggle =====

  const toggleEl = document.getElementById("toggle-auto-edit");
  if (toggleEl) {
    toggleEl.addEventListener("click", () => {
      toggleEl.classList.toggle("on");
      const enabled = toggleEl.classList.contains("on");
      vscode.postMessage({ type: "toggle_auto_edit", enabled });
    });
  }

  // ===== Confirmation UI =====

  function renderConfirmation(data) {
    const block = document.createElement("div");
    block.className = "confirm-block";
    block.setAttribute("data-confirm-id", data.id);

    const toolLabel = document.createElement("span");
    toolLabel.className = "confirm-tool-label";
    toolLabel.textContent = (data.tool || "tool").toUpperCase();

    const desc = document.createElement("span");
    desc.className = "confirm-desc";
    desc.textContent = data.description || "";

    const header = document.createElement("div");
    header.className = "confirm-header";
    header.appendChild(toolLabel);
    header.appendChild(desc);
    block.appendChild(header);

    // Show input preview for bash
    if (data.tool === "bash" && data.input && data.input.command) {
      const preview = document.createElement("div");
      preview.className = "bash-code bash-code-compact";
      preview.textContent = data.input.command;
      block.appendChild(preview);
    }

    // Buttons
    const actions = document.createElement("div");
    actions.className = "confirm-actions";

    const allowBtn = document.createElement("button");
    allowBtn.className = "confirm-btn confirm-allow";
    allowBtn.textContent = "Allow";

    const denyBtn = document.createElement("button");
    denyBtn.className = "confirm-btn confirm-deny";
    denyBtn.textContent = "Deny";

    const allowAllBtn = document.createElement("button");
    allowAllBtn.className = "confirm-btn confirm-allow-all";
    allowAllBtn.textContent = "Allow all";

    allowBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "confirm_response", id: data.id, allowed: true });
      block.remove();
    });

    denyBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "confirm_response", id: data.id, allowed: false });
      block.remove();
    });

    allowAllBtn.addEventListener("click", () => {
      // Turn on auto-edit and approve this one
      if (toggleEl) {
        toggleEl.classList.add("on");
      }
      vscode.postMessage({ type: "toggle_auto_edit", enabled: true });
      vscode.postMessage({ type: "confirm_response", id: data.id, allowed: true });
      block.remove();
    });

    actions.appendChild(allowBtn);
    actions.appendChild(denyBtn);
    actions.appendChild(allowAllBtn);
    block.appendChild(actions);

    messagesEl.appendChild(block);
    scrollToBottom();
  }

  function autoResizeInput() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  // ===== Streaming text =====

  let liveTextEl = null;
  let liveTextContent = "";

  function appendTextDelta(text) {
    // Empty string = finalize the live element (finalizeLiveText saves to history)
    if (text === "") {
      finalizeLiveText();
      return;
    }

    // Clear spinner and restore live text
    if (spinnerEl) {
      spinnerEl.remove();
      spinnerEl = null;
    }

    if (liveTextEl) {
      liveTextEl.style.display = "";
    }

    if (!liveTextEl) {
      // Create the live streaming element on the tree line
      liveTextEl = document.createElement("div");
      liveTextEl.className = "tree-item running msg-agent msg-agent-streaming";

      const contentEl = document.createElement("div");
      contentEl.className = "msg-agent-content";

      liveTextEl.appendChild(contentEl);
      messagesEl.appendChild(liveTextEl);
      liveTextContent = "";
    }

    liveTextContent += text;

    // Render markdown during streaming (throttled to avoid lag)
    const contentEl = liveTextEl.querySelector(".msg-agent-content");
    if (contentEl) {
      if (!appendTextDelta._timer) {
        appendTextDelta._timer = setTimeout(() => {
          appendTextDelta._timer = null;
          if (contentEl && liveTextContent) {
            contentEl.innerHTML = renderMarkdown(liveTextContent);
          }
        }, 100); // Re-render at most every 100ms
      }
    }

    scrollToBottom();
  }

  // ===== Spinner =====

  function showSpinner(text) {
    // Hide any live streaming text while spinner is active
    if (liveTextEl) {
      liveTextEl.style.display = "none";
    }

    if (spinnerEl) {
      spinnerEl.querySelector(".spinner-text").textContent = text;
      return;
    }
    spinnerEl = document.createElement("div");
    spinnerEl.className = "spinner-block";

    const icon = document.createElement("div");
    icon.className = "spinner-icon";

    const textEl = document.createElement("span");
    textEl.className = "spinner-text";
    textEl.textContent = text;

    spinnerEl.appendChild(icon);
    spinnerEl.appendChild(textEl);
    messagesEl.appendChild(spinnerEl);
    scrollToBottom();
  }

  /** Finalize any in-progress streaming text element */
  function finalizeLiveText() {
    if (!liveTextEl) return;

    const content = liveTextContent.trim();
    liveTextEl.remove();
    liveTextEl = null;
    liveTextContent = "";

    if (!content) return;

    // Save the finalized text as an agent message for replay persistence
    recordMessage({ type: "agent", content });

    // Strip <memory> blocks from display
    const displayContent = content.replace(/<memory>[\s\S]*?<\/memory>/g, "").trim();
    if (!displayContent) return;

    // Show as full rendered markdown on the tree line
    const wrapper = document.createElement("div");
    wrapper.className = "tree-item msg-agent";

    const textEl = document.createElement("div");
    textEl.className = "msg-agent-content";
    textEl.innerHTML = renderMarkdown(displayContent);

    wrapper.appendChild(textEl);
    messagesEl.appendChild(wrapper);
    scrollToBottom();
  }

  function clearSpinner() {
    // Don't finalize live text here — only the empty text_delta or agent message does that
    if (spinnerEl) {
      spinnerEl.remove();
      spinnerEl = null;
    }
    // Restore live text visibility if it was hidden
    if (liveTextEl) {
      liveTextEl.style.display = "";
    }
  }

  // ===== Todo List =====

  function renderTodoUpdate(todos) {
    if (!todos || todos.length === 0) {
      if (todoBlockEl) {
        todoBlockEl.remove();
        todoBlockEl = null;
      }
      return;
    }

    const completed = todos.filter((t) => t.status === "completed").length;
    const total = todos.length;
    const inProgress = todos.find((t) => t.status === "in_progress");

    if (!todoBlockEl) {
      todoBlockEl = document.createElement("div");
      todoBlockEl.className = "todo-block";
      messagesEl.appendChild(todoBlockEl);
    }

    todoBlockEl.innerHTML = "";

    // Header
    const header = document.createElement("div");
    header.className = "todo-header";

    const chevron = document.createElement("div");
    chevron.className = "todo-chevron";
    chevron.innerHTML = "&#9660;";

    const title = document.createElement("span");
    title.textContent = "Tasks";

    const progress = document.createElement("span");
    progress.className = "todo-progress";
    progress.textContent = `${completed}/${total} completed`;

    header.appendChild(chevron);
    header.appendChild(title);
    header.appendChild(progress);
    header.addEventListener("click", () => {
      todoBlockEl.classList.toggle("collapsed");
    });

    todoBlockEl.appendChild(header);

    // Body
    const body = document.createElement("div");
    body.className = "todo-body";

    todos.forEach((todo) => {
      const item = document.createElement("div");
      item.className = "todo-item " + todo.status;

      const checkbox = document.createElement("div");
      checkbox.className = "todo-checkbox";
      if (todo.status === "completed") {
        checkbox.innerHTML = "&#10003;";
      } else if (todo.status === "in_progress") {
        checkbox.innerHTML = "&#9654;"; // play arrow
      }

      const label = document.createElement("span");
      label.className = "todo-label";
      label.textContent = todo.content;

      item.appendChild(checkbox);
      item.appendChild(label);
      body.appendChild(item);

      // Show activeForm for in_progress task
      if (todo.status === "in_progress" && todo.activeForm) {
        const active = document.createElement("div");
        active.className = "todo-active-form";

        const activeSpinner = document.createElement("div");
        activeSpinner.className = "todo-active-spinner";

        const activeText = document.createElement("span");
        activeText.textContent = todo.activeForm;

        active.appendChild(activeSpinner);
        active.appendChild(activeText);
        body.appendChild(active);
      }
    });

    todoBlockEl.appendChild(body);
    scrollToBottom();
  }

  // ===== Sub-Agent =====

  function renderSubAgent(data) {
    // Check if we already have a block for this agent_id
    let block = data.agent_id
      ? document.querySelector(`[data-agent-id="${data.agent_id}"]`)
      : null;

    if (block) {
      // Update existing block
      const statusIcon = block.querySelector(".subagent-status-icon");
      if (statusIcon) {
        statusIcon.className = "subagent-status-icon " + (data.status || "done");
        statusIcon.innerHTML = statusIconHtml(data.status || "done");
      }
      if (data.result) {
        let bodyEl = block.querySelector(".subagent-body");
        if (!bodyEl) {
          bodyEl = document.createElement("div");
          bodyEl.className = "subagent-body";
          block.appendChild(bodyEl);
        }
        bodyEl.textContent = data.result;
      }
      return;
    }

    block = document.createElement("div");
    block.className = "subagent-block";
    if (data.agent_id) {
      block.setAttribute("data-agent-id", data.agent_id);
    }

    // Header
    const header = document.createElement("div");
    header.className = "subagent-header";

    const chevron = document.createElement("div");
    chevron.className = "subagent-chevron";
    chevron.innerHTML = "&#9660;";

    const label = document.createElement("span");
    label.className = "subagent-label";
    label.textContent = "Agent";

    const desc = document.createElement("span");
    desc.className = "subagent-desc";
    desc.textContent = data.description || "Sub-agent task";

    const statusIcon = document.createElement("div");
    statusIcon.className = "subagent-status-icon " + (data.status || "running");
    statusIcon.innerHTML = statusIconHtml(data.status || "running");

    header.appendChild(chevron);
    header.appendChild(label);
    header.appendChild(desc);
    header.appendChild(statusIcon);

    header.addEventListener("click", () => {
      block.classList.toggle("collapsed");
    });

    block.appendChild(header);

    // Body (result)
    if (data.result) {
      const bodyEl = document.createElement("div");
      bodyEl.className = "subagent-body";
      bodyEl.textContent = data.result;
      block.appendChild(bodyEl);
    }

    messagesEl.appendChild(block);
    scrollToBottom();
  }

  // ===== Render functions =====

  function renderUserMessage(content) {
    const wrapper = document.createElement("div");
    wrapper.className = "msg-user";

    const label = document.createElement("div");
    label.className = "msg-user-label";
    label.textContent = "You";

    const body = document.createElement("div");
    body.textContent = content;

    wrapper.appendChild(label);
    wrapper.appendChild(body);
    messagesEl.appendChild(wrapper);
    scrollToBottom();
  }

  function renderAgentText(content) {
    // If there's a live streaming element, finalize it
    if (liveTextEl && liveTextContent) {
      if (content === liveTextContent) {
        finalizeLiveText();
        return;
      }
      finalizeLiveText();
    }

    if (!content || !content.trim()) return;

    // Strip <memory> blocks from display — they're auto-ingested by the backend
    const displayContent = content.replace(/<memory>[\s\S]*?<\/memory>/g, "").trim();
    if (!displayContent) return;

    // Show agent response as full rendered markdown on the tree line
    const wrapper = document.createElement("div");
    wrapper.className = "tree-item msg-agent";

    const textEl = document.createElement("div");
    textEl.className = "msg-agent-content";
    textEl.innerHTML = renderMarkdown(displayContent);

    wrapper.appendChild(textEl);
    messagesEl.appendChild(wrapper);
    scrollToBottom();
  }

  function renderError(content) {
    const el = document.createElement("div");
    el.className = "msg-error";
    el.textContent = content;
    messagesEl.appendChild(el);
    scrollToBottom();
  }

  function renderToolCall(data) {
    const tool = data.tool || "bash";
    const status = data.status || "done";

    const block = document.createElement("div");
    block.className = "tool-block";
    block.setAttribute("data-tool", tool);

    // Header with tree dot
    const header = document.createElement("div");
    header.className = "tool-header";

    const dot = document.createElement("div");
    dot.className = "tool-dot " + status;

    const chevron = document.createElement("div");
    chevron.className = "tool-chevron";
    chevron.innerHTML = "▶";

    const label = document.createElement("span");
    label.className = "tool-label";
    label.textContent = toolDisplayName(tool);

    const desc = document.createElement("span");
    desc.className = "tool-desc";
    desc.textContent = buildToolDescription(data);

    header.appendChild(dot);
    header.appendChild(chevron);
    header.appendChild(label);
    header.appendChild(desc);

    header.addEventListener("click", () => {
      block.classList.toggle("collapsed");
    });

    // Body
    const body = document.createElement("div");
    body.className = "tool-body";

    switch (tool) {
      case "bash":
        renderBashBody(body, data);
        break;
      case "read":
        renderReadBody(body, data);
        break;
      case "write":
        renderWriteBody(body, data);
        break;
      case "edit":
        renderEditBody(body, data);
        break;
      case "agent":
        renderAgentToolBody(body, data);
        break;
      case "search":
      case "glob":
      case "grep":
        renderSearchBody(body, data);
        break;
      default:
        renderGenericBody(body, data);
        break;
    }

    block.appendChild(header);
    block.appendChild(body);
    messagesEl.appendChild(block);
    scrollToBottom();
  }

  // ===== Tool body renderers =====
  // Design: compact summaries in chat, click to open full content in VS Code

  let _toolOutputCounter = 0;
  function nextToolId() {
    return (++_toolOutputCounter).toString(36);
  }

  function renderBashBody(body, data) {
    // IN label + command
    if (data.command) {
      const inLabel = document.createElement("div");
      inLabel.className = "bash-section-label";
      inLabel.textContent = "IN";
      body.appendChild(inLabel);

      const cmdBlock = document.createElement("div");
      cmdBlock.className = "bash-code";
      cmdBlock.textContent = data.command;
      body.appendChild(cmdBlock);
    }

    // OUT label + output
    if (data.output != null) {
      const outLabel = document.createElement("div");
      outLabel.className = "bash-section-label";
      outLabel.textContent = "OUT";
      body.appendChild(outLabel);

      if (data.output === "" || data.output === "(Bash completed with no output)") {
        const emptyEl = document.createElement("div");
        emptyEl.className = "bash-empty";
        emptyEl.textContent = "(Bash completed with no output)";
        body.appendChild(emptyEl);
      } else {
        const lines = data.output.split("\n");
        const id = nextToolId();

        // Show preview (first 4 lines max)
        const preview = lines.slice(0, 4).join("\n");
        const outBlock = document.createElement("div");
        outBlock.className = "bash-code bash-code-compact";
        outBlock.textContent = preview;
        if (lines.length > 4) {
          const fade = document.createElement("div");
          fade.className = "bash-code-fade";
          outBlock.appendChild(fade);
        }
        outBlock.style.cursor = "pointer";
        outBlock.title = "Click to view full output";
        outBlock.addEventListener("click", () => {
          vscode.postMessage({
            type: "open_content",
            title: "Bash tool output (" + id + ")",
            content: data.output,
          });
        });
        body.appendChild(outBlock);

        setFilePill("Bash tool output (" + id + ")", data.output);
      }
    }
  }

  function renderReadBody(body, data) {
    if (!data.file_path) return;

    // Show file path + line range
    let pathText = data.file_path;
    if (data.line_range) pathText += " (" + data.line_range + ")";

    const pathEl = document.createElement("a");
    pathEl.className = "tool-output-link";
    pathEl.href = "#";
    pathEl.textContent = pathText;
    pathEl.addEventListener("click", (e) => {
      e.preventDefault();
      vscode.postMessage({ type: "open_file", path: data.file_path });
    });
    body.appendChild(pathEl);

    // Content preview (first 4 lines, click to open full)
    if (data.content) {
      const lines = data.content.split("\n");
      const preview = lines.slice(0, 4).join("\n");
      const outBlock = document.createElement("div");
      outBlock.className = "bash-code bash-code-compact";
      outBlock.textContent = preview;
      if (lines.length > 4) {
        const fade = document.createElement("div");
        fade.className = "bash-code-fade";
        outBlock.appendChild(fade);
      }
      outBlock.style.cursor = "pointer";
      outBlock.title = "Click to open file";
      outBlock.addEventListener("click", () => {
        vscode.postMessage({ type: "open_file", path: data.file_path });
      });
      body.appendChild(outBlock);
    }
  }

  function renderWriteBody(body, data) {
    if (!data.file_path) return;

    // File path as clickable link to open the file
    const pathEl = document.createElement("a");
    pathEl.className = "tool-output-link";
    pathEl.href = "#";
    pathEl.textContent = data.file_path;
    pathEl.addEventListener("click", (e) => {
      e.preventDefault();
      vscode.postMessage({ type: "open_file", path: data.file_path });
    });
    body.appendChild(pathEl);

    if (data.line_count != null) {
      const detail = document.createElement("span");
      detail.className = "write-detail";
      detail.textContent = " " + data.line_count + " lines";
      body.appendChild(detail);
    }
  }

  function renderEditBody(body, data) {
    const oldLines = data.old_lines || [];
    const newLines = data.new_lines || [];

    // Summary
    const added = newLines.length - oldLines.length;
    const summaryText =
      oldLines.length === 0 && newLines.length === 0
        ? (data.added_lines != null
            ? (data.added_lines >= 0 ? "Added " : "Removed ") + Math.abs(data.added_lines) + " lines"
            : "Edit applied")
        : added > 0
          ? "Added " + added + " line" + (added !== 1 ? "s" : "")
          : added < 0
            ? "Removed " + Math.abs(added) + " line" + (Math.abs(added) !== 1 ? "s" : "")
            : "Modified " + oldLines.length + " line" + (oldLines.length !== 1 ? "s" : "");

    const summary = document.createElement("div");
    summary.className = "diff-summary";
    summary.textContent = summaryText;
    body.appendChild(summary);

    // Click to open VS Code diff view
    if (oldLines.length > 0 || newLines.length > 0) {
      const link = document.createElement("a");
      link.className = "tool-output-link";
      link.href = "#";
      link.textContent = "View diff";
      link.addEventListener("click", (e) => {
        e.preventDefault();
        vscode.postMessage({
          type: "open_diff",
          file_path: data.file_path || "edit",
          old_text: oldLines.join("\n"),
          new_text: newLines.join("\n"),
        });
      });
      body.appendChild(link);
    }
  }

  function renderAgentToolBody(body, data) {
    if (data.agent_prompt) {
      const desc = document.createElement("div");
      desc.className = "bash-code bash-code-compact";
      desc.textContent = truncate(data.agent_prompt, 200);
      body.appendChild(desc);
    }
    if (data.agent_result) {
      const id = nextToolId();
      const link = document.createElement("a");
      link.className = "tool-output-link";
      link.href = "#";
      link.textContent = "Agent result (" + id + ")";
      link.addEventListener("click", (e) => {
        e.preventDefault();
        vscode.postMessage({
          type: "open_content",
          title: "Agent result (" + id + ")",
          content: data.agent_result,
        });
      });
      body.appendChild(link);
    }
  }

  function renderSearchBody(body, data) {
    const content = data.content || data.output || "";
    if (!content) return;

    // Tree-like display for file/search results
    const lines = content.split("\n").filter(Boolean);
    if (lines.length <= 15) {
      // Show all inline as tree
      const tree = document.createElement("div");
      tree.className = "file-tree";
      lines.forEach((line) => {
        const item = document.createElement("div");
        item.className = "file-tree-item";
        item.textContent = line;
        tree.appendChild(item);
      });
      body.appendChild(tree);
    } else {
      // Too many results — show count + clickable link
      const countEl = document.createElement("div");
      countEl.className = "write-detail";
      countEl.textContent = lines.length + " results";
      body.appendChild(countEl);

      const id = nextToolId();
      const link = document.createElement("a");
      link.className = "tool-output-link";
      link.href = "#";
      link.textContent = "View full output (" + id + ")";
      link.addEventListener("click", (e) => {
        e.preventDefault();
        vscode.postMessage({
          type: "open_content",
          title: "Search results (" + id + ")",
          content: content,
        });
      });
      body.appendChild(link);
    }
  }

  function renderGenericBody(body, data) {
    const content = data.content || "";
    if (!content) return;

    // Show content inline with preview + click to open full
    const lines = content.split("\n");
    const preview = lines.slice(0, 4).join("\n");
    const outBlock = document.createElement("div");
    outBlock.className = "bash-code" + (lines.length > 4 ? " bash-code-compact" : "");
    outBlock.textContent = preview;
    if (lines.length > 4) {
      const fade = document.createElement("div");
      fade.className = "bash-code-fade";
      outBlock.appendChild(fade);
      outBlock.style.cursor = "pointer";
      outBlock.title = "Click to view full output";
      const id = nextToolId();
      outBlock.addEventListener("click", () => {
        vscode.postMessage({
          type: "open_content",
          title: "Tool output (" + id + ")",
          content: content,
        });
      });
    }
    body.appendChild(outBlock);
  }

  // ===== Markdown rendering (lightweight) =====

  function renderMarkdown(text) {
    if (!text) return "";
    // Process line by line for block-level elements, then inline
    const lines = text.split("\n");
    const output = [];
    let inCodeBlock = false;
    let codeBlockContent = [];
    let inList = false;
    let listType = ""; // "ul" or "ol"

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Code block fences
      if (line.trimStart().startsWith("```")) {
        if (inCodeBlock) {
          output.push(`<pre>${escapeHtml(codeBlockContent.join("\n"))}</pre>`);
          codeBlockContent = [];
          inCodeBlock = false;
        } else {
          closeList();
          inCodeBlock = true;
        }
        continue;
      }

      if (inCodeBlock) {
        codeBlockContent.push(line);
        continue;
      }

      // Headers: # ## ### ####
      const headerMatch = line.match(/^(#{1,4})\s+(.+)$/);
      if (headerMatch) {
        closeList();
        const level = headerMatch[1].length;
        output.push(`<h${level + 1}>${renderInline(escapeHtml(headerMatch[2]))}</h${level + 1}>`);
        continue;
      }

      // Unordered list: - item or * item
      const ulMatch = line.match(/^(\s*)[-*]\s+(.+)$/);
      if (ulMatch) {
        if (!inList || listType !== "ul") {
          closeList();
          output.push("<ul>");
          inList = true;
          listType = "ul";
        }
        output.push(`<li>${renderInline(escapeHtml(ulMatch[2]))}</li>`);
        continue;
      }

      // Ordered list: 1. item
      const olMatch = line.match(/^(\s*)\d+\.\s+(.+)$/);
      if (olMatch) {
        if (!inList || listType !== "ol") {
          closeList();
          output.push("<ol>");
          inList = true;
          listType = "ol";
        }
        output.push(`<li>${renderInline(escapeHtml(olMatch[2]))}</li>`);
        continue;
      }

      // Empty line
      if (line.trim() === "") {
        closeList();
        output.push("<br>");
        continue;
      }

      // Normal paragraph line
      closeList();
      output.push(`<p>${renderInline(escapeHtml(line))}</p>`);
    }

    // Close any open code block or list
    if (inCodeBlock) {
      output.push(`<pre>${escapeHtml(codeBlockContent.join("\n"))}</pre>`);
    }
    closeList();

    return output.join("\n");

    function closeList() {
      if (inList) {
        output.push(listType === "ol" ? "</ol>" : "</ul>");
        inList = false;
        listType = "";
      }
    }
  }

  /** Render inline markdown: bold, italic, code, links */
  function renderInline(html) {
    // Inline code: `...`
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Bold: **...**
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    // Italic: *...*  (but not inside **)
    html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
    // Links: [text](url)
    html = html.replace(
      /\[([^\]]+)\]\(([^)]+)\)/g,
      '<a class="file-link" href="#" data-path="$2">$1</a>'
    );
    return html;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // ===== Helpers =====

  function toolDisplayName(tool) {
    const names = {
      bash: "Bash",
      read: "Read",
      write: "Write",
      edit: "Edit",
      search: "Search",
      glob: "Glob",
      grep: "Grep",
      agent: "Agent",
    };
    return names[tool] || tool.charAt(0).toUpperCase() + tool.slice(1);
  }

  function buildToolDescription(data) {
    if (data.description) {
      return data.description;
    }

    switch (data.tool) {
      case "bash":
        return data.command
          ? truncate(data.command, 60)
          : "Running command...";
      case "read":
        return data.file_path
          ? data.file_path + (data.line_range ? " " + data.line_range : "")
          : "Reading file...";
      case "write":
        return data.file_path || "Writing file...";
      case "edit":
        return data.file_path || "Editing file...";
      case "agent":
        return data.agent_type
          ? data.agent_type
          : "Running sub-agent...";
      case "glob":
      case "grep":
      case "search":
        return data.content
          ? truncate(data.content, 60)
          : "Searching...";
      default:
        return "";
    }
  }

  function truncate(str, max) {
    if (str.length <= max) return str;
    return str.substring(0, max) + "\u2026";
  }

  function statusIconHtml(status) {
    switch (status) {
      case "done":
        return "&#10003;"; // checkmark
      case "running":
        return "&#8987;"; // hourglass
      case "error":
        return "&#10007;"; // X
      default:
        return "&#10003;";
    }
  }

  function updateStatus(content) {
    statusPill.textContent = content;

    statusPill.classList.remove("connected", "error");
    const lower = content.toLowerCase();
    if (lower.includes("connected") && !lower.includes("dis")) {
      statusPill.classList.add("connected");
    } else if (lower.includes("error") || lower.includes("disconnect")) {
      statusPill.classList.add("error");
    }
  }

  // ===== Context usage pie chart =====

  function updateContextPie(used, max, percent) {
    let pie = document.getElementById("context-pie");
    if (!pie) {
      // Create the pie element next to status pill
      pie = document.createElement("div");
      pie.id = "context-pie";
      pie.className = "context-pie";
      pie.title = "";
      // Insert before status pill
      statusPill.parentNode.insertBefore(pie, statusPill);
    }

    // Color: green < 50%, yellow 50-80%, red > 80%
    let color = "#3fb950";
    if (percent > 80) color = "#f85149";
    else if (percent > 50) color = "#d29922";

    // SVG pie using conic-gradient trick via a circle + dasharray
    const size = 16;
    const r = 6;
    const circ = 2 * Math.PI * r;
    const filled = (percent / 100) * circ;

    pie.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
      <circle cx="8" cy="8" r="${r}" fill="none" stroke="rgba(255,255,255,0.1)" stroke-width="3"/>
      <circle cx="8" cy="8" r="${r}" fill="none" stroke="${color}" stroke-width="3"
        stroke-dasharray="${filled} ${circ - filled}"
        stroke-dashoffset="${circ * 0.25}"
        stroke-linecap="round"/>
    </svg>`;

    const usedK = (used / 1000).toFixed(0);
    const maxK = (max / 1000).toFixed(0);
    pie.title = `Context: ${usedK}K / ${maxK}K tokens (${percent}%)`;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ===== File pill in action bar =====

  const filePillsEl = document.getElementById("file-pills");
  let lastToolOutputId = null;
  let lastToolOutputContent = null;
  let lastToolOutputTitle = null;

  function setFilePill(title, content) {
    if (!filePillsEl) return;
    filePillsEl.innerHTML = "";
    if (!title) return;

    lastToolOutputTitle = title;
    lastToolOutputContent = content;

    const pill = document.createElement("div");
    pill.className = "file-pill";

    const icon = document.createElement("span");
    icon.className = "file-pill-icon";
    icon.textContent = "📄";

    const label = document.createElement("span");
    label.textContent = title;

    pill.appendChild(icon);
    pill.appendChild(label);
    pill.addEventListener("click", () => {
      if (lastToolOutputContent) {
        vscode.postMessage({
          type: "open_content",
          title: lastToolOutputTitle,
          content: lastToolOutputContent,
        });
      }
    });

    filePillsEl.appendChild(pill);
  }

  // ===== Stop button =====

  const stopBtn = document.getElementById("btn-stop");
  if (stopBtn) {
    stopBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "stop" });
    });
  }

  // ===== Top bar buttons =====
  const newSessionBtn = document.getElementById("btn-new-session");
  if (newSessionBtn) {
    newSessionBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "new_session" });
    });
  }

  const sessionsBtn = document.getElementById("btn-sessions");
  if (sessionsBtn) {
    sessionsBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "browse_sessions" });
    });
  }

  // Handle file link clicks
  messagesEl.addEventListener("click", (e) => {
    const link = e.target.closest(".file-link");
    if (link) {
      e.preventDefault();
      const path = link.getAttribute("data-path");
      if (path) {
        vscode.postMessage({ type: "open_file", path });
      }
    }
  });
})();
