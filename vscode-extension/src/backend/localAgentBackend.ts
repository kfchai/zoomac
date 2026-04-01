import * as vscode from "vscode";
import type { WebviewMessage, OutboundToolCall } from "../protocol";
import type { Backend } from "./types";
import type {
  ConversationMessage,
  ContentBlock,
  LLMProvider,
  LLMResponse,
  ToolDefinition,
} from "../local/providers/types";
import { createProvider } from "../local/providers/types";
import { TOOL_DEFINITIONS } from "../local/tools";
import { createToolHandlers, type ToolHandler } from "../local/toolHandlers";
import { MemoryBridge } from "../local/memoryBridge";
import { buildSystemPrompt } from "../local/systemPrompt";
import { compactContext, estimateTotalTokens, getMaxContextTokens } from "../local/contextCompactor";

const MAX_ITERATIONS = 200;

/** Tools that modify files or run commands — require confirmation when autoEdit is off */
const DESTRUCTIVE_TOOLS = new Set(["bash", "write", "edit"]);

export class LocalAgentBackend implements Backend {
  private _provider: LLMProvider;
  private _model: string;
  private _maxTokens: number;
  private _workspaceRoot: string;
  private _baseSystemPrompt = "";
  private _messages: ConversationMessage[] = [];
  private _toolHandlers: Record<string, ToolHandler>;
  private _memoryBridge: MemoryBridge;
  private _memoryAvailable = false;
  private _abortController?: AbortController;
  autoEdit = true;

  /** Pending confirmation resolvers keyed by confirmation ID */
  private _pendingConfirmations = new Map<string, (allowed: boolean) => void>();
  private _confirmCounter = 0;

  private readonly _emitter = new vscode.EventEmitter<WebviewMessage>();
  readonly onMessage = this._emitter.event;

  constructor(
    workspaceRoot: string,
    config: {
      provider: string;
      apiKey?: string;
      baseUrl?: string;
      model: string;
      maxTokens: number;
    }
  ) {
    this._workspaceRoot = workspaceRoot;
    this._provider = createProvider(config);
    this._model = config.model;
    this._maxTokens = config.maxTokens;
    // Use a project-scoped, isolated memory directory
    const pathMod = require("path");
    const memoryDir = pathMod.join(workspaceRoot, ".zoomac", "memory");
    this._memoryBridge = new MemoryBridge({
      projectDir: memoryDir,
      workspaceRoot,
    });
    this._toolHandlers = createToolHandlers(workspaceRoot, this._memoryBridge);
  }

  async start(): Promise<void> {
    // Load system prompt (reads ZOOMAC.md / CLAUDE.md / AGENTS.md)
    this._baseSystemPrompt = await buildSystemPrompt(this._workspaceRoot);

    // Initialize memory — tries daemon, subprocess, then MEMORY.md fallback
    const memBackend = await this._memoryBridge.init();
    this._memoryAvailable = true; // Always available now (MEMORY.md fallback)
    const memLabel = memBackend === "daemon" ? "memory (memgate)" :
                     memBackend === "subprocess" ? "memory (memgate)" :
                     "memory (MEMORY.md)";

    this._emitter.fire({
      type: "status",
      content: `Local (${this._model}) · ${memLabel}`,
    });
  }

  async stop(): Promise<void> {
    this._abortController?.abort();
    this._abortController = undefined;
    this._memoryBridge.dispose();
    // Reject all pending confirmations
    for (const [, resolve] of this._pendingConfirmations) {
      resolve(false);
    }
    this._pendingConfirmations.clear();
  }

  /** Called by the provider (chatViewProvider/chatPanel) when user responds to a confirmation */
  resolveConfirmation(id: string, allowed: boolean): void {
    const resolve = this._pendingConfirmations.get(id);
    if (resolve) {
      this._pendingConfirmations.delete(id);
      resolve(allowed);
    }
  }

  /** Ask the user for permission to run a destructive tool. Returns true if allowed. */
  private _requestConfirmation(
    toolName: string,
    toolInput: Record<string, unknown>
  ): Promise<boolean> {
    const id = "confirm_" + (++this._confirmCounter);

    return new Promise<boolean>((resolve) => {
      this._pendingConfirmations.set(id, resolve);

      // Emit confirmation request to webview
      this._emitter.fire({
        type: "confirm_tool",
        id,
        tool: toolName,
        description: this._confirmDescription(toolName, toolInput),
        input: toolInput,
      } as unknown as WebviewMessage);
    });
  }

  private _confirmDescription(tool: string, input: Record<string, unknown>): string {
    switch (tool) {
      case "bash":
        return input.command as string || "Run command";
      case "write":
        return `Write ${input.file_path}`;
      case "edit":
        return `Edit ${input.file_path}`;
      default:
        return `${tool}: ${JSON.stringify(input).substring(0, 80)}`;
    }
  }

  async sendMessageWithImages(
    content: string,
    images: Array<{ mediaType: string; base64: string }>
  ): Promise<void> {
    // Build multimodal content blocks
    const blocks: ContentBlock[] = [];
    for (const img of images) {
      blocks.push({
        type: "image" as any,
        source: {
          type: "base64",
          media_type: img.mediaType,
          data: img.base64,
        },
      } as any);
    }
    if (content) {
      blocks.push({ type: "text", text: content });
    }

    // Append to history
    this._messages.push({ role: "user", content: blocks });

    // Show thinking spinner
    this._emitter.fire({
      type: "spinner",
      text: "Thinking...",
      active: true,
    } as WebviewMessage);

    try {
      await this._injectMemoryContext(content || "image input");
      await this._runToolLoop();
    } catch (err: unknown) {
      this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
      const msg = err instanceof Error ? err.message : String(err);
      this._emitter.fire({ type: "error", content: `Error: ${msg}` });
    }
  }

  async sendMessage(content: string): Promise<void> {
    // Echo user message
    this._emitter.fire({ type: "user", content });

    // Append to conversation history
    this._messages.push({ role: "user", content });

    // Show thinking spinner
    this._emitter.fire({
      type: "spinner",
      text: "Thinking...",
      active: true,
    } as WebviewMessage);

    try {
      // Auto-retrieve relevant memories before calling the LLM
      await this._injectMemoryContext(content);
      await this._runToolLoop();
    } catch (err: unknown) {
      this._emitter.fire({
        type: "spinner",
        text: "",
        active: false,
      } as WebviewMessage);

      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("aborted")) {
        this._emitter.fire({ type: "error", content: "Request cancelled." });
      } else {
        this._emitter.fire({ type: "error", content: `Error: ${msg}` });
      }
    }
  }

  private async _runToolLoop(): Promise<void> {
    for (let iteration = 0; iteration < MAX_ITERATIONS; iteration++) {
      // Compact context if approaching the model's limit
      await this._compactIfNeeded();

      // Clear spinner — streaming text will appear directly
      this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);

      // Start a live streaming text element in the webview
      let streamedText = "";
      let streamStarted = false;

      const onStreamEvent = (event: import("../local/providers/types").StreamEvent) => {
        if (event.type === "text_delta") {
          if (!streamStarted) {
            // Create the live text element
            this._emitter.fire({ type: "text_delta", text: event.text } as unknown as WebviewMessage);
            streamStarted = true;
          } else {
            this._emitter.fire({ type: "text_delta", text: event.text } as unknown as WebviewMessage);
          }
          streamedText += event.text;
        }
      };

      // Call LLM with streaming
      const response = await this._callProviderStreaming(onStreamEvent);

      // Finalize the streamed text (tell webview to close the live element)
      if (streamStarted) {
        this._emitter.fire({ type: "text_delta", text: "" } as unknown as WebviewMessage);
      }

      // Collect tool use blocks
      const toolUses: ContentBlock[] = [];
      for (const block of response.content) {
        if (block.type === "tool_use") {
          toolUses.push(block);
        }
      }

      // If no tool calls, we're done
      if (response.stopReason !== "tool_use" || toolUses.length === 0) {
        // If no streaming happened (fallback), emit text normally
        if (!streamStarted) {
          const textParts = response.content
            .filter((b) => b.type === "text" && b.text)
            .map((b) => b.text!);
          if (textParts.length > 0) {
            this._emitter.fire({ type: "agent", content: textParts.join("\n") });
          }
        }

        // Append assistant response to history
        this._messages.push({ role: "assistant", content: response.content });

        // Show token usage + context pie
        this._emitContextUsage();
        if (response.usage) {
          this._emitter.fire({
            type: "status",
            content: `Local (${this._model}) · ${response.usage.inputTokens + response.usage.outputTokens} tokens`,
          });
        }

        return;
      }

      // Append assistant response to history (with tool_use blocks)
      this._messages.push({
        role: "assistant",
        content: response.content,
      });

      // Execute each tool call
      const toolResults: ContentBlock[] = [];

      for (const tu of toolUses) {
        const toolName = tu.name || "unknown";
        const toolInput = tu.input || {};

        // Show spinner with tool name
        this._emitter.fire({
          type: "spinner",
          text: this._toolSpinnerText(toolName, toolInput),
          active: true,
        } as WebviewMessage);

        // Check if confirmation needed for destructive tools
        if (!this.autoEdit && DESTRUCTIVE_TOOLS.has(toolName)) {
          // Clear spinner while waiting for user
          this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);

          const allowed = await this._requestConfirmation(toolName, toolInput);
          if (!allowed) {
            toolResults.push({
              type: "tool_result",
              tool_use_id: tu.id,
              content: `User denied: ${toolName} was not executed.`,
            });
            this._emitter.fire({
              type: "agent",
              content: `Skipped ${toolName} (denied by user).`,
            });
            continue;
          }

          // Re-show spinner after approval
          this._emitter.fire({
            type: "spinner",
            text: this._toolSpinnerText(toolName, toolInput),
            active: true,
          } as WebviewMessage);
        }

        // Execute the tool
        const handler = this._toolHandlers[toolName];
        let result: string;

        if (!handler) {
          result = `Error: unknown tool '${toolName}'`;
        } else {
          try {
            result = await handler(toolInput);
          } catch (err: unknown) {
            result = `Error: ${err instanceof Error ? err.message : String(err)}`;
          }
        }

        // Clear spinner and emit tool call result
        this._emitter.fire({
          type: "spinner",
          text: "",
          active: false,
        } as WebviewMessage);

        this._emitToolCall(toolName, toolInput, result);

        // Truncate large tool results before sending to LLM to save tokens
        const trimmedResult = this._trimToolResult(result, toolName);

        toolResults.push({
          type: "tool_result",
          tool_use_id: tu.id,
          content: trimmedResult,
        });
      }

      // Append tool results to history
      this._messages.push({
        role: "user",
        content: toolResults,
      });

      this._emitContextUsage();

      // Loop continues — call the LLM again with tool results
    }

    // Max iterations reached — this shouldn't happen in practice with 200
    this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
    this._emitter.fire({
      type: "agent",
      content: `Reached ${MAX_ITERATIONS} tool loop iterations — stopping to avoid runaway. You can send another message to continue.`,
    });
  }

  /** Compact conversation context if approaching the model's token limit. */
  private async _compactIfNeeded(): Promise<void> {
    const systemPrompt = this._currentSystemPrompt || this._baseSystemPrompt;
    const maxTokens = getMaxContextTokens(this._model);
    const currentTokens = estimateTotalTokens(systemPrompt, this._messages);

    // Only attempt compaction if we're over 70% of the limit
    if (currentTokens < maxTokens * 0.7) {
      return;
    }

    this._emitter.fire({
      type: "spinner",
      text: "Compacting context...",
      active: true,
    } as WebviewMessage);

    try {
      const result = await compactContext(
        this._provider,
        this._model,
        systemPrompt,
        this._messages,
        maxTokens
      );

      if (result) {
        this._messages = result.messages;
        this._emitter.fire({
          type: "agent",
          content: `*Context compacted: ${result.compactedCount} messages summarized, ~${result.tokensSaved} tokens saved.*`,
        });
      }
    } catch {
      // Compaction failed — continue with full context, may hit limit
    }

    this._emitter.fire({
      type: "spinner",
      text: "Thinking...",
      active: true,
    } as WebviewMessage);
  }

  /** Auto-retrieve relevant memories and enrich the system prompt. */
  private async _injectMemoryContext(userMessage: string): Promise<void> {
    if (!this._memoryAvailable) {
      this._currentSystemPrompt = this._baseSystemPrompt;
      return;
    }

    try {
      const context = await this._memoryBridge.retrieveContext(userMessage);
      if (context) {
        this._currentSystemPrompt =
          this._baseSystemPrompt +
          "\n\n## Relevant Memories\n\n" +
          "The following context was retrieved from long-term memory. " +
          "Use it to inform your response, but verify if needed.\n\n" +
          context;
      } else {
        this._currentSystemPrompt = this._baseSystemPrompt;
      }
    } catch {
      // Memory retrieval failed — proceed without it
      this._currentSystemPrompt = this._baseSystemPrompt;
    }
  }

  private _currentSystemPrompt = "";

  private async _callProvider(): Promise<LLMResponse> {
    const systemPrompt = this._currentSystemPrompt || this._baseSystemPrompt;

    const provider = this._provider as {
      createMessageWithModel?: (
        model: string,
        system: string,
        messages: ConversationMessage[],
        tools: ToolDefinition[],
        maxTokens: number
      ) => Promise<LLMResponse>;
      createMessage: (
        system: string,
        messages: ConversationMessage[],
        tools: ToolDefinition[],
        maxTokens: number
      ) => Promise<LLMResponse>;
    };

    const call = () =>
      provider.createMessageWithModel
        ? provider.createMessageWithModel(
            this._model, systemPrompt, this._messages, TOOL_DEFINITIONS, this._maxTokens
          )
        : provider.createMessage(
            systemPrompt, this._messages, TOOL_DEFINITIONS, this._maxTokens
          );

    // Retry with backoff on rate limit (429)
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        return await call();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        const status = (err as { status?: number }).status;

        if (status === 429 || msg.includes("rate_limit") || msg.includes("429")) {
          // Parse retry-after or use exponential backoff
          const waitSec = Math.pow(2, attempt + 1) * 5; // 10s, 20s, 40s
          this._emitter.fire({
            type: "spinner",
            text: `Rate limited — waiting ${waitSec}s...`,
            active: true,
          } as WebviewMessage);
          await new Promise((r) => setTimeout(r, waitSec * 1000));
          continue;
        }

        // Not a rate limit error — rethrow
        throw err;
      }
    }

    throw new Error("Rate limit: max retries exceeded. Try again in a minute.");
  }

  /** Streaming version of _callProvider — emits text_delta events as tokens arrive */
  private async _callProviderStreaming(
    onEvent: (event: import("../local/providers/types").StreamEvent) => void
  ): Promise<LLMResponse> {
    const systemPrompt = this._currentSystemPrompt || this._baseSystemPrompt;

    const provider = this._provider as {
      createMessageStreamWithModel?: (
        model: string,
        system: string,
        messages: ConversationMessage[],
        tools: ToolDefinition[],
        maxTokens: number,
        onEvent: (event: import("../local/providers/types").StreamEvent) => void
      ) => Promise<LLMResponse>;
      createMessageWithModel?: (
        model: string,
        system: string,
        messages: ConversationMessage[],
        tools: ToolDefinition[],
        maxTokens: number
      ) => Promise<LLMResponse>;
      createMessage: (
        system: string,
        messages: ConversationMessage[],
        tools: ToolDefinition[],
        maxTokens: number
      ) => Promise<LLMResponse>;
    };

    // Try streaming first, fall back to non-streaming
    const call = () => {
      if (provider.createMessageStreamWithModel) {
        return provider.createMessageStreamWithModel(
          this._model, systemPrompt, this._messages, TOOL_DEFINITIONS, this._maxTokens, onEvent
        );
      }
      if (provider.createMessageWithModel) {
        return provider.createMessageWithModel(
          this._model, systemPrompt, this._messages, TOOL_DEFINITIONS, this._maxTokens
        );
      }
      return provider.createMessage(
        systemPrompt, this._messages, TOOL_DEFINITIONS, this._maxTokens
      );
    };

    // Retry with backoff on rate limit
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        return await call();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        const status = (err as { status?: number }).status;

        if (status === 429 || msg.includes("rate_limit") || msg.includes("429")) {
          const waitSec = Math.pow(2, attempt + 1) * 5;
          this._emitter.fire({
            type: "spinner",
            text: `Rate limited — waiting ${waitSec}s...`,
            active: true,
          } as WebviewMessage);
          await new Promise((r) => setTimeout(r, waitSec * 1000));
          continue;
        }
        throw err;
      }
    }

    throw new Error("Rate limit: max retries exceeded. Try again in a minute.");
  }

  private _emitToolCall(
    toolName: string,
    input: Record<string, unknown>,
    result: string
  ): void {
    const data: Partial<OutboundToolCall> = {
      type: "tool_call",
      tool: toolName as OutboundToolCall["tool"],
      status: result.startsWith("Error") ? "error" : "done",
    };

    switch (toolName) {
      case "bash":
        data.command = input.command as string;
        data.output = result;
        data.description = truncate(input.command as string, 60);
        break;
      case "read":
        data.file_path = input.file_path as string;
        data.line_range = input.offset != null
          ? `lines ${input.offset}-${(input.offset as number) + ((input.limit as number) || 100)}`
          : undefined;
        data.content = result;
        data.description = input.file_path as string;
        break;
      case "write":
        data.file_path = input.file_path as string;
        data.line_count = (input.content as string)?.split("\n").length;
        data.description = input.file_path as string;
        break;
      case "edit":
        data.file_path = input.file_path as string;
        data.old_lines = (input.old_string as string)?.split("\n");
        data.new_lines = (input.new_string as string)?.split("\n");
        data.description = input.file_path as string;
        break;
      case "glob":
        data.content = result;
        data.description = input.pattern as string;
        break;
      case "grep":
        data.content = result;
        data.description = input.pattern as string;
        break;
      default:
        data.content = result;
        break;
    }

    this._emitter.fire({ type: "tool_call", data: data as OutboundToolCall } as WebviewMessage);
  }

  /**
   * Trim large tool results before sending to LLM.
   * The full result is shown in the UI (via _emitToolCall above),
   * but only a truncated version goes into the conversation history.
   */
  private _trimToolResult(result: string, toolName: string): string {
    // Don't trim short results
    if (result.length <= 3000) return result;

    // For file reads: keep first and last portion
    if (toolName === "read") {
      const lines = result.split("\n");
      if (lines.length > 100) {
        const head = lines.slice(0, 60).join("\n");
        const tail = lines.slice(-20).join("\n");
        return head + `\n\n... [${lines.length - 80} lines omitted] ...\n\n` + tail;
      }
    }

    // For grep/search: limit to first N matches
    if (toolName === "grep" || toolName === "search") {
      const lines = result.split("\n");
      if (lines.length > 50) {
        return lines.slice(0, 50).join("\n") + `\n... [${lines.length - 50} more lines]`;
      }
    }

    // General truncation
    if (result.length > 6000) {
      return result.substring(0, 5000) + `\n... [truncated, ${result.length} chars total]`;
    }

    return result;
  }

  /** Emit context usage to the webview for the pie chart indicator. */
  private _emitContextUsage(): void {
    const systemPrompt = this._currentSystemPrompt || this._baseSystemPrompt;
    const used = estimateTotalTokens(systemPrompt, this._messages);
    const max = getMaxContextTokens(this._model);
    const percent = Math.min(100, Math.round((used / max) * 100));
    this._emitter.fire({
      type: "context_usage",
      used,
      max,
      percent,
    } as unknown as WebviewMessage);
  }

  private _toolSpinnerText(
    name: string,
    input: Record<string, unknown>
  ): string {
    switch (name) {
      case "bash":
        return `Running: ${truncate(input.command as string || "", 50)}`;
      case "read":
        return `Reading ${input.file_path}`;
      case "write":
        return `Writing ${input.file_path}`;
      case "edit":
        return `Editing ${input.file_path}`;
      case "glob":
        return `Searching for ${input.pattern}`;
      case "grep":
        return `Searching: ${input.pattern}`;
      default:
        return `Running ${name}...`;
    }
  }
}

function truncate(str: string, max: number): string {
  if (str.length <= max) return str;
  return str.substring(0, max) + "\u2026";
}
