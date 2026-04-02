/**
 * Lightweight MCP (Model Context Protocol) client.
 * Connects to MCP servers via stdio and exposes their tools
 * as regular tool handlers for the agent.
 */

import { ChildProcess, spawn } from "child_process";
import type { ToolDefinition } from "./providers/types";

export interface McpServerConfig {
  /** Display name */
  name: string;
  /** Command to start the server */
  command: string;
  /** Arguments */
  args?: string[];
  /** Environment variables */
  env?: Record<string, string>;
}

interface McpTool {
  name: string;
  description: string;
  inputSchema: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
  };
}

export class McpClient {
  private _process: ChildProcess | null = null;
  private _buffer = "";
  private _pendingRequests = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  private _requestId = 0;
  private _tools: McpTool[] = [];
  private _serverName: string;

  constructor(private _config: McpServerConfig) {
    this._serverName = _config.name;
  }

  get name(): string { return this._serverName; }

  /** Start the MCP server and discover its tools. */
  async connect(): Promise<ToolDefinition[]> {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error(`MCP server '${this._serverName}' startup timeout`));
      }, 15000);

      try {
        this._process = spawn(
          this._config.command,
          this._config.args || [],
          {
            stdio: ["pipe", "pipe", "pipe"],
            env: { ...process.env, ...this._config.env },
            shell: true,
          }
        );

        this._process.stdout!.on("data", (data: Buffer) => {
          this._buffer += data.toString();
          this._processBuffer();
        });

        this._process.stderr!.on("data", () => {
          // Ignore stderr
        });

        this._process.on("exit", () => {
          this._process = null;
          for (const [, req] of this._pendingRequests) {
            req.reject(new Error("MCP server exited"));
          }
          this._pendingRequests.clear();
        });

        this._process.on("error", (err) => {
          clearTimeout(timeout);
          reject(err);
        });

        // Initialize the MCP connection
        this._sendRequest("initialize", {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "zoomac", version: "0.2.0" },
        }).then(async () => {
          // Send initialized notification
          this._sendNotification("notifications/initialized", {});

          // List available tools
          const result = await this._sendRequest("tools/list", {});
          this._tools = (result as { tools: McpTool[] }).tools || [];

          clearTimeout(timeout);
          resolve(this._tools.map((t) => this._toToolDefinition(t)));
        }).catch((err) => {
          clearTimeout(timeout);
          reject(err);
        });
      } catch (err) {
        clearTimeout(timeout);
        reject(err);
      }
    });
  }

  /** Call a tool on the MCP server. */
  async callTool(name: string, args: Record<string, unknown>): Promise<string> {
    const result = await this._sendRequest("tools/call", {
      name,
      arguments: args,
    });

    const resp = result as { content?: Array<{ type: string; text?: string }> };
    if (resp.content) {
      return resp.content
        .filter((c) => c.type === "text" && c.text)
        .map((c) => c.text!)
        .join("\n");
    }
    return JSON.stringify(result);
  }

  /** Disconnect from the MCP server. */
  disconnect(): void {
    if (this._process) {
      this._process.kill();
      this._process = null;
    }
  }

  /** Convert MCP tool to our ToolDefinition format. */
  private _toToolDefinition(tool: McpTool): ToolDefinition {
    return {
      name: `mcp_${this._serverName}_${tool.name}`,
      description: `[${this._serverName}] ${tool.description}`,
      input_schema: tool.inputSchema || { type: "object", properties: {} },
    };
  }

  /** Create tool handlers for all MCP tools. */
  createHandlers(): Record<string, (input: Record<string, unknown>) => Promise<string>> {
    const handlers: Record<string, (input: Record<string, unknown>) => Promise<string>> = {};
    for (const tool of this._tools) {
      const toolName = tool.name;
      handlers[`mcp_${this._serverName}_${toolName}`] = async (input) => {
        return this.callTool(toolName, input);
      };
    }
    return handlers;
  }

  // ── JSON-RPC over stdio ──

  private _sendRequest(method: string, params: unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this._process) {
        reject(new Error("MCP server not running"));
        return;
      }

      const id = ++this._requestId;
      const timer = setTimeout(() => {
        this._pendingRequests.delete(id);
        reject(new Error(`MCP request timeout: ${method}`));
      }, 30000);

      this._pendingRequests.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });

      const msg = JSON.stringify({ jsonrpc: "2.0", id, method, params });
      this._process.stdin!.write(msg + "\n");
    });
  }

  private _sendNotification(method: string, params: unknown): void {
    if (!this._process) return;
    const msg = JSON.stringify({ jsonrpc: "2.0", method, params });
    this._process.stdin!.write(msg + "\n");
  }

  private _processBuffer(): void {
    const lines = this._buffer.split("\n");
    this._buffer = lines.pop() || "";

    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.id != null && this._pendingRequests.has(msg.id)) {
          const req = this._pendingRequests.get(msg.id)!;
          this._pendingRequests.delete(msg.id);
          if (msg.error) {
            req.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
          } else {
            req.resolve(msg.result);
          }
        }
      } catch {
        // Non-JSON line
      }
    }
  }
}

/**
 * Load MCP server configs from workspace settings.
 * Config format in .vscode/settings.json:
 * "zoomac.mcpServers": {
 *   "browser": { "command": "npx", "args": ["-y", "@anthropic-ai/mcp-server-browser"] },
 *   "postgres": { "command": "npx", "args": ["-y", "@anthropic-ai/mcp-server-postgres", "postgresql://..."] }
 * }
 */
export function loadMcpConfigs(): McpServerConfig[] {
  try {
    const vscode = require("vscode");
    const config = vscode.workspace.getConfiguration("zoomac");
    const servers: Record<string, any> = config.get("mcpServers") || {};

    return Object.entries(servers).map(([name, cfg]: [string, any]) => ({
      name,
      command: cfg.command as string,
      args: cfg.args as string[] | undefined,
      env: cfg.env as Record<string, string> | undefined,
    }));
  } catch {
    return [];
  }
}
