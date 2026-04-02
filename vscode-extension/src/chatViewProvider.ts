import * as vscode from "vscode";
import type { Backend } from "./backend/types";
import { WebSocketBackend } from "./backend/websocketBackend";
import { LocalAgentBackend } from "./backend/localAgentBackend";

export class ChatViewProvider implements vscode.WebviewViewProvider {
  private _view?: vscode.WebviewView;
  private _backend?: Backend;
  private _configListener?: vscode.Disposable;

  constructor(private readonly _extensionUri: vscode.Uri) {}

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ) {
    this._view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this._extensionUri, "media"),
      ],
    };

    webviewView.webview.html = this._getHtmlContent(webviewView.webview);

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage((data) => {
      if (data.type === "send_with_images" && this._backend) {
        (this._backend as any).sendMessageWithImages?.(data.content, data.images)
          || this._backend.sendMessage(data.content);
      } else if (data.type === "send" && data.content) {
        this._backend?.sendMessage(data.content);
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
      }
    });

    // Start the backend
    this._createAndStartBackend();

    // Listen for config changes to switch backends
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

    webviewView.onDidDispose(() => {
      this._backend?.stop();
      this._configListener?.dispose();
    });
  }

  private async _createAndStartBackend(): Promise<void> {
    // Stop existing backend
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

      // Validate API key for non-Ollama providers
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

    // Subscribe to backend messages → forward to webview
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
    // Config change listener will handle the restart
  }

  private _openFile(filePath: string): void {
    const uri = vscode.Uri.file(filePath);
    vscode.window.showTextDocument(uri, { preview: true }).then(
      undefined,
      () => {
        const folders = vscode.workspace.workspaceFolders;
        if (folders && folders.length > 0) {
          const absUri = vscode.Uri.joinPath(folders[0].uri, filePath);
          vscode.window.showTextDocument(absUri, { preview: true });
        }
      }
    );
  }

  /** Open tool output as a readonly temp file (like CC's "Bash tool output (id)") */
  private async _openContentAsFile(title: string, content: string): Promise<void> {
    const doc = await vscode.workspace.openTextDocument({
      content,
      language: "plaintext",
    });
    await vscode.window.showTextDocument(doc, {
      preview: true,
      viewColumn: vscode.ViewColumn.Beside,
    });
  }

  /** Open a diff view comparing old and new text for a file */
  private async _openDiff(
    filePath: string,
    oldText: string,
    newText: string
  ): Promise<void> {
    const oldDoc = await vscode.workspace.openTextDocument({
      content: oldText,
      language: this._guessLanguage(filePath),
    });
    const newDoc = await vscode.workspace.openTextDocument({
      content: newText,
      language: this._guessLanguage(filePath),
    });
    const title = `${filePath} (edit diff)`;
    await vscode.commands.executeCommand("vscode.diff", oldDoc.uri, newDoc.uri, title);
  }

  private _guessLanguage(filePath: string): string {
    const ext = filePath.split(".").pop() || "";
    const map: Record<string, string> = {
      ts: "typescript", tsx: "typescriptreact",
      js: "javascript", jsx: "javascriptreact",
      py: "python", rs: "rust", go: "go",
      java: "java", css: "css", html: "html",
      json: "json", yaml: "yaml", yml: "yaml",
      md: "markdown", sh: "shellscript", bash: "shellscript",
    };
    return map[ext] || "plaintext";
  }

  private _postToWebview(data: Record<string, unknown>) {
    this._view?.webview.postMessage(data);
  }

  private _getHtmlContent(webview: vscode.Webview): string {
    const cssUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "media", "chat.css")
    );
    const jsUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "media", "chat.js")
    );

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${cssUri}">
</head>
<body>
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
