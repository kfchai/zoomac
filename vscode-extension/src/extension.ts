import * as vscode from "vscode";
import { ChatPanel } from "./chatPanel";
import { ChatViewProvider } from "./chatViewProvider";
import { SessionStore } from "./sessionStore";

let log: vscode.OutputChannel;

export function activate(context: vscode.ExtensionContext) {
  log = vscode.window.createOutputChannel("Zoomac");
  log.appendLine("[activate] Zoomac extension activating...");

  // Initialize session store with workspace storage
  const store = new SessionStore(context.workspaceState);
  ChatPanel.init(store, context.extensionUri, log);

  const sessions = store.listSessions();
  const lastActive = store.getLastActive();
  log.appendLine(`[activate] Sessions: ${sessions.length}, lastActive: ${lastActive || "none"}`);
  for (const s of sessions) {
    log.appendLine(`  - ${s.id}: "${s.title}" (${s.messageCount} msgs, updated ${new Date(s.updatedAt).toISOString()})`);
  }

  // Register serializer — VS Code calls this to restore panels after reload
  context.subscriptions.push(
    vscode.window.registerWebviewPanelSerializer("zoomac.chat", {
      async deserializeWebviewPanel(
        panel: vscode.WebviewPanel,
        state: unknown
      ) {
        log.appendLine(`[serializer] deserializeWebviewPanel called, state: ${JSON.stringify(state)}`);
        try {
          ChatPanel.restore(panel, (state as { sessionId?: string }) || {});
          log.appendLine("[serializer] restore completed");
        } catch (err) {
          log.appendLine(`[serializer] ERROR: ${err}`);
          panel.dispose();
        }
      },
    })
  );

  // New chat (Ctrl+Shift+Z)
  context.subscriptions.push(
    vscode.commands.registerCommand("zoomac.openChat", () => {
      log.appendLine("[command] openChat");
      ChatPanel.openNew(context.extensionUri);
    })
  );

  // Resume previous session
  context.subscriptions.push(
    vscode.commands.registerCommand("zoomac.resumeSession", () => {
      log.appendLine("[command] resumeSession");
      ChatPanel.showSessionPicker(context.extensionUri);
    })
  );

  // Sidebar view (still available for quick access)
  const sidebarProvider = new ChatViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("zoomac.chatView", sidebarProvider)
  );

  // Mode toggle command
  context.subscriptions.push(
    vscode.commands.registerCommand("zoomac.switchMode", async () => {
      const config = vscode.workspace.getConfiguration("zoomac");
      const current = config.get<string>("mode") || "local";
      const next = current === "local" ? "remote" : "local";
      await config.update("mode", next, vscode.ConfigurationTarget.Workspace);
      vscode.window.showInformationMessage(`Zoomac: Switched to ${next} mode`);
    })
  );

  log.appendLine("[activate] done");
}

export function deactivate() {}
