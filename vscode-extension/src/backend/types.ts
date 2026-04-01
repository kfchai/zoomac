import * as vscode from "vscode";
import type { WebviewMessage } from "../protocol";

/**
 * Abstraction over how the extension communicates with an LLM backend.
 * Both WebSocket (remote Docker) and Local (direct API) implement this.
 */
export interface Backend {
  /** Initialize the backend (connect WebSocket, validate API key, etc.) */
  start(): Promise<void>;

  /** Tear down the backend cleanly */
  stop(): Promise<void>;

  /** Send a user message to the backend for processing */
  sendMessage(content: string): Promise<void>;

  /** Resolve a pending tool confirmation (allow/deny) */
  resolveConfirmation?(id: string, allowed: boolean): void;

  /** Whether destructive tools auto-execute without confirmation */
  autoEdit: boolean;

  /** Event fired when the backend has a message for the webview */
  readonly onMessage: vscode.Event<WebviewMessage>;
}
