/** Message types for the Zoomac WebSocket protocol. */

// --- Inbound (extension -> agent) ---

export interface InboundMessage {
  type: "message";
  channel: string;
  content: string;
  metadata?: Record<string, unknown>;
}

// --- Outbound (agent -> extension) ---

export type OutboundMessage =
  | OutboundConnected
  | OutboundResponse
  | OutboundToolCall
  | OutboundTodoUpdate
  | OutboundSpinner
  | OutboundSubAgent
  | OutboundStatus
  | OutboundError;

export interface OutboundConnected {
  type: "connected";
  channel: string;
  content: string;
}

export interface OutboundResponse {
  type: "response";
  channel?: string;
  content: string;
  reply_to?: string;
  metadata?: Record<string, unknown>;
}

export interface OutboundToolCall {
  type: "tool_call";
  tool: "bash" | "read" | "edit" | "write" | "search" | "glob" | "grep" | "agent";
  description?: string;
  status?: "running" | "done" | "error";
  // bash
  command?: string;
  output?: string;
  // read
  file_path?: string;
  line_range?: string;
  content?: string;
  // write
  line_count?: number;
  // edit
  old_lines?: string[];
  new_lines?: string[];
  added_lines?: number;
  // agent (sub-agent)
  agent_type?: string;
  agent_prompt?: string;
  agent_result?: string;
}

export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm?: string;
}

export interface OutboundTodoUpdate {
  type: "todo_update";
  todos: TodoItem[];
}

export interface OutboundSpinner {
  type: "spinner";
  text: string;
  active: boolean;
}

export interface OutboundSubAgent {
  type: "sub_agent";
  agent_id: string;
  description: string;
  status: "running" | "done" | "error";
  result?: string;
}

export interface OutboundStatus {
  type: "status";
  content: string;
}

export interface OutboundError {
  type: "error";
  content: string;
}

// --- Webview messages (extension -> webview) ---

export type WebviewMessage =
  | { type: "user"; content: string }
  | { type: "agent"; content: string }
  | { type: "tool_call"; data: OutboundToolCall }
  | { type: "todo_update"; todos: TodoItem[] }
  | { type: "spinner"; text: string; active: boolean }
  | { type: "sub_agent"; data: OutboundSubAgent }
  | { type: "confirm_tool"; id: string; tool: string; description: string; input: Record<string, unknown> }
  | { type: "text_delta"; text: string }
  | { type: "context_usage"; used: number; max: number; percent: number }
  | { type: "token_usage"; input: number; output: number; cacheRead: number; cacheWrite: number; totalInput: number; totalOutput: number; totalCacheRead: number; totalCacheWrite: number; cost: number; totalCost: number; apiCalls: number }
  | { type: "status"; content: string }
  | { type: "error"; content: string };
