import * as vscode from "vscode";
import WebSocket from "ws";
import type { InboundMessage, OutboundMessage, WebviewMessage } from "../protocol";
import type { Backend } from "./types";

export class WebSocketBackend implements Backend {
  private _ws?: WebSocket;
  private _channel = "";
  private _reconnectDelay = 1000;
  private _maxReconnectDelay = 30000;
  private _reconnectTimer?: NodeJS.Timeout;

  private readonly _emitter = new vscode.EventEmitter<WebviewMessage>();
  readonly onMessage = this._emitter.event;
  autoEdit = true; // Remote backend handles its own approval

  constructor(private readonly _wsUrl: string) {}

  async start(): Promise<void> {
    this._connect();
  }

  async stop(): Promise<void> {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = undefined;
    }
    if (this._ws) {
      this._ws.close();
      this._ws = undefined;
    }
  }

  async sendMessage(content: string): Promise<void> {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
      this._emitter.fire({ type: "error", content: "Not connected to agent" });
      return;
    }

    const msg: InboundMessage = {
      type: "message",
      channel: this._channel,
      content,
    };
    this._ws.send(JSON.stringify(msg));
    this._emitter.fire({ type: "user", content });
  }

  private _connect() {
    try {
      this._ws = new WebSocket(this._wsUrl);

      this._ws.on("open", () => {
        this._reconnectDelay = 1000;
        this._emitter.fire({ type: "status", content: "Connecting..." });
      });

      this._ws.on("message", (raw: WebSocket.RawData) => {
        try {
          const msg = JSON.parse(raw.toString()) as OutboundMessage;

          switch (msg.type) {
            case "connected":
              this._channel = msg.channel || "";
              this._emitter.fire({
                type: "status",
                content: `Connected (${this._channel})`,
              });
              break;
            case "response":
              this._emitter.fire({ type: "agent", content: msg.content });
              break;
            case "tool_call":
              this._emitter.fire({ type: "tool_call", data: msg } as WebviewMessage);
              break;
            case "todo_update":
              this._emitter.fire({ type: "todo_update", todos: msg.todos } as WebviewMessage);
              break;
            case "spinner":
              this._emitter.fire({
                type: "spinner",
                text: msg.text,
                active: msg.active,
              } as WebviewMessage);
              break;
            case "sub_agent":
              this._emitter.fire({ type: "sub_agent", data: msg } as WebviewMessage);
              break;
            case "error":
              this._emitter.fire({ type: "error", content: msg.content });
              break;
            case "status":
              this._emitter.fire({ type: "status", content: msg.content });
              break;
          }
        } catch {
          // Ignore parse errors
        }
      });

      this._ws.on("close", () => {
        this._emitter.fire({ type: "status", content: "Disconnected" });
        this._scheduleReconnect();
      });

      this._ws.on("error", () => {
        this._emitter.fire({ type: "status", content: "Connection error" });
        this._scheduleReconnect();
      });
    } catch {
      this._scheduleReconnect();
    }
  }

  private _scheduleReconnect() {
    if (this._reconnectTimer) {
      return;
    }
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = undefined;
      this._reconnectDelay = Math.min(
        this._reconnectDelay * 2,
        this._maxReconnectDelay
      );
      this._connect();
    }, this._reconnectDelay);
  }
}
