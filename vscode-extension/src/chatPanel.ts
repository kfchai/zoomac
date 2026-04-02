import * as vscode from "vscode";
import type { Backend } from "./backend/types";
import { WebSocketBackend } from "./backend/websocketBackend";
import { LocalAgentBackend } from "./backend/localAgentBackend";
import { SessionStore } from "./sessionStore";

/**
 * Opens the Zoomac chat as a full editor panel (main area).
 * Supports multiple concurrent sessions — each panel has its own session ID.
 */
export class ChatPanel {
  /** All active panels keyed by session ID */
  private static _panels = new Map<string, ChatPanel>();
  private static _store: SessionStore;

  private _panel: vscode.WebviewPanel;
  private _sessionId: string;
  private _backend?: Backend;
  private _configListener?: vscode.Disposable;
  private _disposed = false;
  private _messages: unknown[] = [];

  private static _extensionUri: vscode.Uri;
  private static _log: vscode.OutputChannel | undefined;

  static init(store: SessionStore, extensionUri: vscode.Uri, log?: vscode.OutputChannel) {
    ChatPanel._store = store;
    ChatPanel._extensionUri = extensionUri;
    ChatPanel._log = log;
  }

  /**
   * Called by the serializer when VS Code restores a panel after reload.
   * The panel already exists — we just need to wire it up with the right session.
   */
  static restore(panel: vscode.WebviewPanel, state: { sessionId?: string }) {
    const log = ChatPanel._log;
    log?.appendLine(`[restore] called, state.sessionId=${state?.sessionId}`);
    log?.appendLine(`[restore] _extensionUri=${ChatPanel._extensionUri?.toString()}, _store=${!!ChatPanel._store}`);

    if (!ChatPanel._extensionUri || !ChatPanel._store) {
      log?.appendLine("[restore] BAIL: no extensionUri or store");
      panel.dispose();
      return;
    }

    const sessionId = state?.sessionId || ChatPanel._store.getLastActive();
    log?.appendLine(`[restore] resolved sessionId=${sessionId}`);
    if (!sessionId) {
      log?.appendLine("[restore] BAIL: no sessionId");
      panel.dispose();
      return;
    }

    const data = ChatPanel._store.loadSession(sessionId);
    log?.appendLine(`[restore] loadSession: ${data ? data.messages.length + " messages" : "NOT FOUND"}`);
    if (!data) {
      log?.appendLine("[restore] BAIL: session data not found");
      panel.dispose();
      return;
    }

    panel.title = data.meta.title || "Zoomac";
    log?.appendLine(`[restore] creating ChatPanel for "${data.meta.title}" with ${data.messages.length} messages`);
    const instance = new ChatPanel(
      ChatPanel._extensionUri,
      panel,
      sessionId,
      data.messages
    );
    ChatPanel._panels.set(sessionId, instance);
    log?.appendLine("[restore] done");
  }

  private constructor(
    private readonly _extensionUri: vscode.Uri,
    panel: vscode.WebviewPanel,
    sessionId: string,
    existingMessages?: unknown[]
  ) {
    this._panel = panel;
    this._sessionId = sessionId;
    this._messages = existingMessages || [];

    // Set options — enableScripts is required, localResourceRoots defaults to extension dir
    try {
      panel.webview.options = {
        enableScripts: true,
        localResourceRoots: [this._extensionUri],
      };
      ChatPanel._log?.appendLine(`[constructor] webview options set, enableScripts=${panel.webview.options.enableScripts}`);
    } catch (e) {
      ChatPanel._log?.appendLine(`[constructor] WARNING: failed to set webview options: ${e}`);
    }

    const html = this._getHtmlContent(panel.webview);
    ChatPanel._log?.appendLine(`[constructor] setting HTML (${html.length} chars), enableScripts=${panel.webview.options.enableScripts}`);
    panel.webview.html = html;

    panel.webview.onDidReceiveMessage((data) => {
      if (data.type === "webview_ready") {
        ChatPanel._log?.appendLine(`[webview_ready] received, messages to replay: ${this._messages.length}`);
        if (this._messages.length > 0) {
          // Restore webview UI
          this._postToWebview({
            type: "restore_session",
            messages: this._messages,
          });
          // Restore LLM conversation history so the agent has context
          this._backend?.restoreHistory?.(this._messages);
          ChatPanel._log?.appendLine(`[webview_ready] backend history restored`);
        }
        return;
      }
      if (data.type === "send_with_images" && this._backend) {
        this._updateTitle(data.content || "Image");
        (this._backend as any).sendMessageWithImages?.(data.content, data.images)
          || this._backend.sendMessage(data.content);
      } else if (data.type === "new_session") {
        ChatPanel.openNew(this._extensionUri);
      } else if (data.type === "browse_sessions") {
        ChatPanel.showSessionPicker(this._extensionUri);
      } else if (data.type === "send" && data.content) {
        this._updateTitle(data.content);
        const enriched = this._enrichWithSelection(data.content);
        this._backend?.sendMessage(enriched);
      } else if (data.type === "open_file" && data.path) {
        this._openFile(data.path);
      } else if (data.type === "open_content" && data.content) {
        this._openContentAsFile(data.title || "output", data.content);
      } else if (data.type === "open_diff" && data.file_path) {
        this._openDiff(data.file_path, data.old_text, data.new_text);
      } else if (data.type === "confirm_response" && data.id) {
        this._backend?.resolveConfirmation?.(data.id, !!data.allowed);
      } else if (data.type === "prompt_response" && data.id) {
        (this._backend as any)?.resolvePrompt?.(data.id, data.answer || "");
      } else if (data.type === "toggle_auto_edit") {
        if (this._backend) {
          this._backend.autoEdit = !!data.enabled;
        }
      } else if (data.type === "switch_mode") {
        this._toggleMode();
      } else if (data.type === "stop") {
        (this._backend as any)?.cancel?.() || this._backend?.stop();
      } else if (data.type === "save_history") {
        // Webview sends its current message list for persistence
        this._messages = data.messages || [];
        this._persistSession();
      }
    });

    this._createAndStartBackend();

    this._configListener = vscode.workspace.onDidChangeConfiguration((e) => {
      if (
        e.affectsConfiguration("zoomac.mode") ||
        e.affectsConfiguration("zoomac.provider") ||
        e.affectsConfiguration("zoomac.model") ||
        e.affectsConfiguration("zoomac.apiKey") ||
        e.affectsConfiguration("zoomac.baseUrl")
      ) {
        this._createAndStartBackend();
      }
    });

    // Track as last active session
    ChatPanel._store?.setLastActive(this._sessionId);

    panel.onDidChangeViewState(() => {
      if (panel.visible) {
        ChatPanel._store?.setLastActive(this._sessionId);
      }
    });

    panel.onDidDispose(() => {
      this._disposed = true;
      this._backend?.stop();
      this._configListener?.dispose();
      ChatPanel._panels.delete(this._sessionId);
    });

    // Messages will be replayed when webview sends "webview_ready"
    ChatPanel._log?.appendLine(`[constructor] waiting for webview_ready (${this._messages.length} messages to replay)`);
  }

  /** Auto-restore the last active session on reload, or open new if none exists */
  static restoreOrNew(extensionUri: vscode.Uri): void {
    if (!ChatPanel._store) {
      ChatPanel.openNew(extensionUri);
      return;
    }

    const lastId = ChatPanel._store.getLastActive();
    if (lastId) {
      const data = ChatPanel._store.loadSession(lastId);
      if (data && data.messages.length > 0) {
        ChatPanel.openSession(extensionUri, lastId);
        return;
      }
    }

    // No previous session — open fresh
    ChatPanel.openNew(extensionUri);
  }

  /** Open a new chat session */
  static openNew(extensionUri: vscode.Uri): void {
    const sessionId = SessionStore.newId();
    ChatPanel._createPanel(extensionUri, sessionId, "Zoomac", []);
  }

  /** Open an existing session by ID */
  static openSession(extensionUri: vscode.Uri, sessionId: string): void {
    // If already open, just reveal
    const existing = ChatPanel._panels.get(sessionId);
    if (existing) {
      existing._panel.reveal(vscode.ViewColumn.One);
      return;
    }

    const data = ChatPanel._store.loadSession(sessionId);
    if (!data) {
      vscode.window.showErrorMessage(`Session not found: ${sessionId}`);
      return;
    }

    ChatPanel._createPanel(
      extensionUri,
      sessionId,
      data.meta.title,
      data.messages
    );
  }

  /** Show quick pick to select a previous session */
  static async showSessionPicker(extensionUri: vscode.Uri): Promise<void> {
    const sessions = ChatPanel._store.listSessions();

    if (sessions.length === 0) {
      vscode.window.showInformationMessage("No previous sessions.");
      return;
    }

    const items = sessions.map((s) => ({
      label: s.title || "Untitled",
      description: `${s.messageCount} messages`,
      detail: new Date(s.updatedAt).toLocaleString(),
      sessionId: s.id,
    }));

    const picked = await vscode.window.showQuickPick(items, {
      placeHolder: "Select a session to resume",
      matchOnDescription: true,
      matchOnDetail: true,
    });

    if (picked) {
      ChatPanel.openSession(extensionUri, picked.sessionId);
    }
  }

  private static _createPanel(
    extensionUri: vscode.Uri,
    sessionId: string,
    title: string,
    messages: unknown[]
  ): void {
    const panel = vscode.window.createWebviewPanel(
      "zoomac.chat",
      title,
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(extensionUri, "media"),
        ],
      }
    );

    const instance = new ChatPanel(extensionUri, panel, sessionId, messages);
    ChatPanel._panels.set(sessionId, instance);
  }

  /** If the user has text selected in an editor, attach it as context. */
  private _enrichWithSelection(content: string): string {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.selection.isEmpty) return content;

    const selection = editor.document.getText(editor.selection);
    if (!selection.trim()) return content;

    const filePath = vscode.workspace.asRelativePath(editor.document.uri);
    const startLine = editor.selection.start.line + 1;
    const endLine = editor.selection.end.line + 1;

    return content + `\n\n<selection file="${filePath}" lines="${startLine}-${endLine}">\n${selection}\n</selection>`;
  }

  private _updateTitle(firstMessage: string) {
    if (this._messages.length === 0) {
      const title = firstMessage.length > 40
        ? firstMessage.substring(0, 40) + "…"
        : firstMessage;
      this._panel.title = title;
      // Update the top bar title in the webview
      this._postToWebview({ type: "update_title", title });
    }
  }

  private async _persistSession(): Promise<void> {
    if (!ChatPanel._store) return;

    const firstUserMsg = this._messages.find(
      (m: any) => m.type === "user"
    ) as { content?: string } | undefined;

    await ChatPanel._store.saveSession({
      meta: {
        id: this._sessionId,
        title: firstUserMsg?.content?.substring(0, 60) || "Untitled",
        createdAt: this._messages.length > 0
          ? (this._messages[0] as any)._ts || Date.now()
          : Date.now(),
        updatedAt: Date.now(),
        messageCount: this._messages.length,
      },
      messages: this._messages,
    });
  }

  // ── Backend setup (unchanged) ──

  private async _createAndStartBackend(): Promise<void> {
    await this._backend?.stop();

    const config = vscode.workspace.getConfiguration("zoomac");
    const mode = config.get<string>("mode") || "local";

    if (mode === "remote") {
      const wsUrl = config.get<string>("wsUrl") || "ws://localhost:8765";
      this._backend = new WebSocketBackend(wsUrl);
    } else {
      const provider = config.get<string>("provider") || "anthropic";
      const apiKey =
        config.get<string>("apiKey") ||
        process.env.ANTHROPIC_API_KEY ||
        process.env.OPENAI_API_KEY ||
        "";
      const baseUrl = config.get<string>("baseUrl") || undefined;
      const model = config.get<string>("model") || "claude-sonnet-4-20250514";
      const maxTokens = config.get<number>("maxTokens") || 8192;

      const workspaceRoot =
        vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();

      if (provider !== "ollama" && !apiKey) {
        this._postToWebview({
          type: "error",
          content:
            "No API key configured. Set zoomac.apiKey in settings or ANTHROPIC_API_KEY / OPENAI_API_KEY env variable.",
        });
        return;
      }

      this._backend = new LocalAgentBackend(workspaceRoot, {
        provider,
        apiKey,
        baseUrl,
        model,
        maxTokens,
      });
    }

    this._backend.onMessage((msg) => {
      this._postToWebview(msg);
    });

    await this._backend.start();
  }

  private async _toggleMode(): Promise<void> {
    const config = vscode.workspace.getConfiguration("zoomac");
    const current = config.get<string>("mode") || "local";
    const next = current === "local" ? "remote" : "local";
    await config.update("mode", next, vscode.ConfigurationTarget.Workspace);
  }

  private _openFile(filePath: string): void {
    const uri = vscode.Uri.file(filePath);
    vscode.window.showTextDocument(uri, {
      preview: true,
      viewColumn: vscode.ViewColumn.Beside,
    }).then(undefined, () => {
      const folders = vscode.workspace.workspaceFolders;
      if (folders && folders.length > 0) {
        const absUri = vscode.Uri.joinPath(folders[0].uri, filePath);
        vscode.window.showTextDocument(absUri, {
          preview: true,
          viewColumn: vscode.ViewColumn.Beside,
        });
      }
    });
  }

  private async _openContentAsFile(title: string, content: string): Promise<void> {
    const doc = await vscode.workspace.openTextDocument({ content, language: "plaintext" });
    await vscode.window.showTextDocument(doc, {
      preview: true,
      viewColumn: vscode.ViewColumn.Beside,
    });
  }

  private async _openDiff(filePath: string, oldText: string, newText: string): Promise<void> {
    const ext = filePath.split(".").pop() || "";
    const langMap: Record<string, string> = {
      ts: "typescript", js: "javascript", py: "python",
      rs: "rust", go: "go", json: "json", css: "css",
    };
    const lang = langMap[ext] || "plaintext";
    const oldDoc = await vscode.workspace.openTextDocument({ content: oldText, language: lang });
    const newDoc = await vscode.workspace.openTextDocument({ content: newText, language: lang });
    await vscode.commands.executeCommand("vscode.diff", oldDoc.uri, newDoc.uri, `${filePath} (edit diff)`);
  }

  private _postToWebview(data: Record<string, unknown>) {
    if (!this._disposed) {
      this._panel.webview.postMessage(data);
    }
  }

  private _getHtmlContent(webview: vscode.Webview): string {
    const cssUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "media", "chat.css")
    );
    const jsUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "media", "chat.js")
    );

    ChatPanel._log?.appendLine(`[getHtml] cssUri=${cssUri}, jsUri=${jsUri}`);

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${cssUri}">
</head>
<body data-session-id="${this._sessionId}">
  <div id="top-bar">
    <button id="btn-new-session" title="New session">+</button>
    <div id="session-title">${(this._messages.length > 0 ? "Session" : "New Session")}</div>
    <div class="spacer"></div>
    <button id="btn-sessions" title="Browse sessions">&#x1f4cb;</button>
  </div>
  <div id="messages"></div>
  <div id="input-area">
    <div id="input-row">
      <textarea id="input" rows="1" placeholder="Queue another message..."></textarea>
    </div>
    <div id="action-bar">
      <button class="action-btn" id="btn-add" title="Add context">+</button>
      <button class="action-btn" id="btn-terminal" title="Terminal">&#9633;</button>
      <div id="status-pill"></div>
      <div id="file-pills"></div>
      <div class="spacer"></div>
      <label class="toggle-row" title="Edit automatically">
        <span class="code-icon">&lt;/&gt;</span>
        <span>Edit automatically</span>
        <span class="toggle-switch on" id="toggle-auto-edit"></span>
      </label>
      <button class="action-btn" id="btn-stop" title="Stop">
        <span class="stop-icon"></span>
      </button>
    </div>
  </div>
  <script src="${jsUri}"></script>
</body>
</html>`;
  }
}
