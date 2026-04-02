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
import { McpClient, loadMcpConfigs } from "../local/mcpClient";
import { MemoryBridge } from "../local/memoryBridge";
import { gatherProjectContext } from "../local/projectContext";
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
  private _mcpClients: McpClient[] = [];
  private _thinkingBudget = 4096;
  private _cancelled = false;
  private _outputChannel: { appendLine: (msg: string) => void } | null = null;
  autoEdit = true;

  /** Cumulative token usage for the session */
  private _totalInputTokens = 0;
  private _totalOutputTokens = 0;
  private _totalCacheReadTokens = 0;
  private _totalCacheWriteTokens = 0;
  private _apiCalls = 0;

  /** Pending confirmation resolvers keyed by confirmation ID */
  private _pendingConfirmations = new Map<string, (allowed: boolean) => void>();
  /** Pending user prompt resolvers */
  private _pendingPrompts = new Map<string, (answer: string) => void>();
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
    // Load thinking budget from settings (0 = disabled)
    try {
      const vscodeSettings = require("vscode").workspace.getConfiguration("zoomac");
      this._thinkingBudget = (vscodeSettings.get("thinkingBudget") as number) ?? 4096;
    } catch {
      this._thinkingBudget = 4096;
    }
    // Use a project-scoped, isolated memory directory
    const pathMod = require("path");
    const memoryDir = pathMod.join(workspaceRoot, ".zoomac", "memory");
    this._memoryBridge = new MemoryBridge({
      projectDir: memoryDir,
      workspaceRoot,
    });
    this._toolHandlers = createToolHandlers(workspaceRoot, this._memoryBridge);

    // Initialize output channel for debugging
    try {
      const vscodeModule = require("vscode");
      this._outputChannel = vscodeModule.window.createOutputChannel("Zoomac Agent");
    } catch {}

    // Register sub-agent tool handler
    this._toolHandlers.agent = this._runSubAgent.bind(this);

    // Register ask_user tool handler
    this._toolHandlers.ask_user = async (input: Record<string, unknown>) => {
      const question = input.question as string;
      const options = (input.options as string[]) || [];
      // Clear spinner while waiting for user
      this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
      const answer = await this._askUser(question, options);
      return `User answered: ${answer}`;
    };
  }

  /** Sub-agent: spawns a separate LLM call with read-only tools for research tasks. */
  private async _runSubAgent(input: Record<string, unknown>): Promise<string> {
    const prompt = input.prompt as string;
    if (!prompt) return "Error: no prompt provided";

    // Build a minimal set of read-only tools for the sub-agent
    const subTools: ToolDefinition[] = TOOL_DEFINITIONS.filter((t) =>
      ["read", "glob", "grep", "bash"].includes(t.name)
    );

    // Emit sub-agent status to webview
    this._emitter.fire({
      type: "sub_agent",
      data: { agent_id: "sub_" + Date.now(), description: prompt.substring(0, 80), status: "running" },
    } as unknown as WebviewMessage);

    const systemPrompt =
      `You are a research sub-agent. Your job is to explore the codebase and answer the question concisely.\n` +
      `Workspace: ${this._workspaceRoot}\n` +
      `Use tools to find information. Return a concise summary of your findings (under 500 words).\n` +
      `Do NOT make any changes — read-only exploration only.`;

    const subMessages: ConversationMessage[] = [
      { role: "user", content: prompt },
    ];

    // Run a mini tool loop (max 10 iterations)
    for (let i = 0; i < 10; i++) {
      const provider = this._provider as any;
      const response: LLMResponse = provider.createMessageWithModel
        ? await provider.createMessageWithModel(this._model, systemPrompt, subMessages, subTools, 4096)
        : await provider.createMessage(systemPrompt, subMessages, subTools, 4096);

      const textParts: string[] = [];
      const toolUses: ContentBlock[] = [];

      for (const block of response.content) {
        if (block.type === "text" && block.text) textParts.push(block.text);
        else if (block.type === "tool_use") toolUses.push(block);
      }

      if (response.stopReason !== "tool_use" || toolUses.length === 0) {
        return textParts.join("\n") || "No findings.";
      }

      subMessages.push({ role: "assistant", content: response.content });

      // Execute sub-agent tools (read-only, no confirmation needed)
      const results: ContentBlock[] = [];
      for (const tu of toolUses) {
        const handler = this._toolHandlers[tu.name || ""];
        let result = "Error: unknown tool";
        if (handler && tu.name !== "agent") { // Prevent recursive sub-agents
          try {
            result = await handler(tu.input || {});
            result = this._trimToolResult(result, tu.name || "");
          } catch (e: unknown) {
            result = `Error: ${e instanceof Error ? e.message : e}`;
          }
        }
        results.push({ type: "tool_result", tool_use_id: tu.id, content: result });
      }

      subMessages.push({ role: "user", content: results });
    }

    return "Sub-agent reached iteration limit.";
  }

  async start(): Promise<void> {
    // Load system prompt (reads ZOOMAC.md / CLAUDE.md / AGENTS.md)
    this._baseSystemPrompt = await buildSystemPrompt(this._workspaceRoot);

    // Auto-detect project context (directory tree, configs, git)
    try {
      const projectCtx = await gatherProjectContext(this._workspaceRoot);
      if (projectCtx) {
        this._baseSystemPrompt += projectCtx;
      }
    } catch {
      // Project context is best-effort
    }

    // Connect MCP servers (external tools)
    const mcpConfigs = loadMcpConfigs();
    for (const cfg of mcpConfigs) {
      try {
        const client = new McpClient(cfg);
        const mcpTools = await client.connect();
        this._mcpClients.push(client);
        // Register MCP tool handlers alongside built-in tools
        const handlers = client.createHandlers();
        Object.assign(this._toolHandlers, handlers);
        // Add MCP tool definitions to TOOL_DEFINITIONS for the LLM
        TOOL_DEFINITIONS.push(...mcpTools);
      } catch {
        // MCP server failed to connect — skip silently
      }
    }

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

  /** Cancel the current in-flight request (stop button). */
  cancel(): void {
    this._cancelled = true;
    // Reject pending confirmations and prompts so the loop unblocks
    for (const [, resolve] of this._pendingConfirmations) {
      resolve(false);
    }
    this._pendingConfirmations.clear();
    for (const [, resolve] of this._pendingPrompts) {
      resolve("[cancelled]");
    }
    this._pendingPrompts.clear();
  }

  async stop(): Promise<void> {
    this.cancel();
    this._memoryBridge.dispose();
    for (const client of this._mcpClients) {
      client.disconnect();
    }
    this._mcpClients = [];
  }

  /**
   * Restore LLM conversation history from saved session messages.
   * Converts webview messages (user, agent, tool_call) back into
   * the ConversationMessage format the LLM expects.
   */
  restoreHistory(messages: unknown[]): void {
    this._messages = [];

    for (const msg of messages) {
      const m = msg as Record<string, any>;
      if (!m || !m.type) continue;

      if (m.type === "user") {
        this._messages.push({ role: "user", content: m.content || "" });
      } else if (m.type === "agent") {
        this._messages.push({
          role: "assistant",
          content: [{ type: "text", text: m.content || "" }],
        });
      } else if (m.type === "tool_call" && m.data) {
        // Tool calls need to be represented as assistant tool_use + user tool_result pairs
        const toolName = m.data.tool || "unknown";
        const toolId = "restored_" + Math.random().toString(36).substring(2, 8);

        // Build input from the saved data
        const input: Record<string, unknown> = {};
        if (m.data.command) input.command = m.data.command;
        if (m.data.file_path) input.file_path = m.data.file_path;
        if (m.data.content) input.query = m.data.content;

        // Assistant's tool_use
        this._messages.push({
          role: "assistant",
          content: [{
            type: "tool_use",
            id: toolId,
            name: toolName,
            input,
          }],
        });

        // Tool result
        const result = m.data.output || m.data.content || "Done";
        this._messages.push({
          role: "user",
          content: [{
            type: "tool_result",
            tool_use_id: toolId,
            content: typeof result === "string" ? result.substring(0, 2000) : "Done",
          }],
        });
      }
    }
  }

  /** Called by the provider (chatViewProvider/chatPanel) when user responds to a confirmation */
  resolveConfirmation(id: string, allowed: boolean): void {
    const resolve = this._pendingConfirmations.get(id);
    if (resolve) {
      this._pendingConfirmations.delete(id);
      resolve(allowed);
    }
  }

  /** Resolve a pending user prompt (from ask_user tool). */
  resolvePrompt(id: string, answer: string): void {
    const resolve = this._pendingPrompts.get(id);
    if (resolve) {
      this._pendingPrompts.delete(id);
      resolve(answer);
    }
  }

  /** Ask the user a question with optional choices. Returns their answer. */
  private _askUser(question: string, options: string[]): Promise<string> {
    const id = "prompt_" + (++this._confirmCounter);

    return new Promise<string>((resolve) => {
      this._pendingPrompts.set(id, resolve);

      this._emitter.fire({
        type: "ask_user",
        id,
        question,
        options: options || [],
      } as unknown as WebviewMessage);
    });
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

  /** Handle slash commands — returns true if handled. */
  private async _handleSlashCommand(content: string): Promise<boolean> {
    const cmd = content.split(/\s+/)[0].toLowerCase();
    const args = content.substring(cmd.length).trim();

    switch (cmd) {
      case "/commit": {
        this._emitter.fire({ type: "user", content });
        this._emitter.fire({ type: "spinner", text: "Generating commit...", active: true } as WebviewMessage);

        const commitPrompt =
          "The user wants to commit their changes. " +
          "Run `git diff --stat` and `git status --short` to see what changed, " +
          "then generate a concise commit message. " +
          "Stage relevant files (skip .env, secrets, lock files) " +
          "and run `git commit -m \"...\"`. " +
          (args ? `User note: ${args}` : "");

        this._messages.push({ role: "user", content: commitPrompt });
        try {
          await this._injectMemoryContext("git commit");
          await this._runToolLoop();
        } catch (err: unknown) {
          this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
          this._emitter.fire({ type: "error", content: `Commit failed: ${err}` });
        }
        return true;
      }

      case "/clear": {
        this._messages = [];
        this._emitter.fire({ type: "agent", content: "Conversation cleared." });
        return true;
      }

      case "/compact": {
        this._emitter.fire({ type: "user", content });
        this._emitter.fire({ type: "spinner", text: "Compacting...", active: true } as WebviewMessage);
        await this._compactIfNeeded();
        this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
        this._emitContextUsage();
        return true;
      }

      case "/model": {
        if (args) {
          this._model = args;
          this._emitter.fire({ type: "agent", content: `Model switched to \`${args}\`.` });
          this._emitter.fire({ type: "status", content: `Local (${this._model})` });
        } else {
          this._emitter.fire({ type: "agent", content: `Current model: \`${this._model}\`` });
        }
        return true;
      }

      case "/review": {
        this._emitter.fire({ type: "user", content });
        this._emitter.fire({ type: "spinner", text: "Reviewer analyzing...", active: true } as WebviewMessage);

        try {
          const review = await this._runReviewer(args);

          // Show the review in the chat with a distinct style
          this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
          this._emitter.fire({
            type: "agent",
            content: "### 🔍 Reviewer Agent\n\n" + review,
          });

          // Inject the review into the main conversation so the agent sees it
          this._messages.push({
            role: "user",
            content: `[REVIEWER FEEDBACK — a second agent reviewed your recent response and actions. Consider this feedback carefully and adjust your approach if needed.]\n\n${review}`,
          });

        } catch (err: unknown) {
          this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
          this._emitter.fire({ type: "error", content: `Review failed: ${err}` });
        }
        return true;
      }

      case "/plan": {
        if (!args) {
          this._emitter.fire({ type: "agent", content: "Usage: `/plan <task description>`\n\nThe agent will create a plan for your review before executing." });
          return true;
        }
        this._emitter.fire({ type: "user", content });
        this._emitter.fire({ type: "spinner", text: "Creating plan...", active: true } as WebviewMessage);

        // Ask the LLM to create a plan WITHOUT executing anything
        const planPrompt =
          "The user wants you to plan before executing. " +
          "Create a detailed step-by-step plan for the following task. " +
          "Use tools (read, glob, grep) to explore the codebase and understand what's needed, " +
          "but do NOT make any changes yet (no write, edit, or bash that modifies files). " +
          "After exploration, output a numbered plan with:\n" +
          "1. Each step described clearly\n" +
          "2. Which files will be modified\n" +
          "3. What changes will be made\n" +
          "4. Any risks or considerations\n\n" +
          "End your response with: **Awaiting approval. Reply 'go' to execute or suggest changes.**\n\n" +
          `Task: ${args}`;

        this._messages.push({ role: "user", content: planPrompt });
        try {
          await this._injectMemoryContext(args);
          await this._runToolLoop();
        } catch (err: unknown) {
          this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
          this._emitter.fire({ type: "error", content: `Plan failed: ${err}` });
        }
        return true;
      }

      case "/help": {
        this._emitter.fire({ type: "agent", content:
          "### Commands\n" +
          "- `/plan <task>` — Plan before executing (explore, then confirm)\n" +
          "- `/commit [note]` — Auto-commit with generated message\n" +
          "- `/review [focus]` — Get a 2nd opinion from a reviewer agent\n" +
          "- `/clear` — Clear conversation\n" +
          "- `/compact` — Force context compaction\n" +
          "- `/model [name]` — Show/switch model\n" +
          "- `/help` — This help"
        });
        return true;
      }

      default:
        return false;
    }
  }

  /** Run a reviewer agent that critiques the main agent's recent work. */
  private async _runReviewer(focus: string): Promise<string> {
    const reviewerSystemPrompt =
      "You are a senior code reviewer and technical critic. " +
      "Your job is to review the conversation and the main agent's recent proposals, replies, and actions. " +
      "Be constructive but honest. Point out:\n" +
      "- Bugs, logic errors, or edge cases the agent missed\n" +
      "- Security concerns or bad practices\n" +
      "- Better approaches or alternatives\n" +
      "- Missing error handling or test coverage\n" +
      "- Unnecessary complexity or over-engineering\n" +
      "- Things the agent did well (acknowledge good work)\n\n" +
      "Be concise — 3-5 bullet points max. No fluff. If everything looks good, say so briefly.\n" +
      "You are NOT the main agent — do not execute any actions or propose code. Only review.";

    // Build conversation summary for the reviewer
    // Include the last N messages for context
    const recentMessages = this._messages.slice(-20);

    // Flatten to a readable summary
    const conversationSummary = recentMessages.map((msg) => {
      if (typeof msg.content === "string") {
        return `[${msg.role}]: ${msg.content.substring(0, 1000)}`;
      }
      const blocks = msg.content as ContentBlock[];
      const parts: string[] = [];
      for (const b of blocks) {
        if (b.type === "text" && b.text) {
          parts.push(b.text.substring(0, 500));
        } else if (b.type === "tool_use") {
          parts.push(`[tool: ${b.name}(${JSON.stringify(b.input || {}).substring(0, 200)})]`);
        } else if (b.type === "tool_result") {
          parts.push(`[result: ${(b.content || "").substring(0, 200)}]`);
        }
      }
      return `[${msg.role}]: ${parts.join(" ")}`;
    }).join("\n\n");

    const reviewPrompt = focus
      ? `Review the agent's recent work with focus on: ${focus}\n\nConversation:\n${conversationSummary}`
      : `Review the agent's recent proposals, code changes, and actions:\n\nConversation:\n${conversationSummary}`;

    // Call the LLM with the reviewer prompt (separate context, no tools)
    const provider = this._provider as any;
    const response: LLMResponse = provider.createMessageWithModel
      ? await provider.createMessageWithModel(this._model, reviewerSystemPrompt, [
          { role: "user", content: reviewPrompt },
        ], [], 2048)
      : await provider.createMessage(reviewerSystemPrompt, [
          { role: "user", content: reviewPrompt },
        ], [], 2048);

    const text = response.content
      .filter((b) => b.type === "text" && b.text)
      .map((b) => b.text!)
      .join("\n");

    return text || "No review feedback generated.";
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
    // Reset cancellation flag on new message
    this._cancelled = false;

    // Handle slash commands
    if (content.startsWith("/")) {
      const handled = await this._handleSlashCommand(content);
      if (handled) return;
    }

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
      this._log(`[sendMessage] "${content.substring(0, 80)}", model=${this._model}, messages=${this._messages.length}`);
      await this._injectMemoryContext(content);
      await this._runToolLoop();
    } catch (err: unknown) {
      this._log(`[sendMessage] ERROR: ${err}`);
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
    // Compact once at the start of the turn, not every iteration
    await this._compactIfNeeded();

    for (let iteration = 0; iteration < MAX_ITERATIONS; iteration++) {
      // Check if cancelled
      if (this._cancelled) {
        this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);
        this._emitter.fire({ type: "agent", content: "*Interrupted.*" });
        return;
      }

      // Clear spinner — streaming text will appear directly
      this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);

      // Start a live streaming text element in the webview
      let streamedText = "";
      let streamStarted = false;

      const onStreamEvent = (event: import("../local/providers/types").StreamEvent) => {
        if (event.type === "text_delta") {
          if (!streamStarted) {
            this._emitter.fire({ type: "text_delta", text: event.text } as unknown as WebviewMessage);
            streamStarted = true;
          } else {
            this._emitter.fire({ type: "text_delta", text: event.text } as unknown as WebviewMessage);
          }
          streamedText += event.text;
        }
      };

      // Only enable thinking on the first call (planning), not during tool loop iterations
      const enableThinking = iteration === 0;

      // Call LLM
      this._log(`[toolLoop] iteration=${iteration}, calling provider...`);
      let response: LLMResponse;
      try {
        response = await this._callProviderStreaming(onStreamEvent, enableThinking);
        this._log(`[toolLoop] response: stopReason=${response.stopReason}, blocks=${response.content.length}, streamStarted=${streamStarted}`);
        for (const b of response.content) {
          this._log(`[toolLoop]   block: type=${b.type}, text=${b.text?.substring(0, 80) || ""}, name=${b.name || ""}`);
        }
      } catch (err: unknown) {
        this._log(`[toolLoop] ERROR: ${err}`);
        throw err;
      }

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
          this._log(`[toolLoop] final text parts: ${textParts.length}, total chars: ${textParts.join("").length}`);
          if (textParts.length > 0) {
            this._emitter.fire({ type: "agent", content: textParts.join("\n") });
          } else {
            this._log(`[toolLoop] WARNING: no text in response and no tool calls`);
          }
        }

        // Append assistant response to history (filter empty text blocks)
        const cleanContent = response.content.filter(
          (b) => !(b.type === "text" && (!b.text || b.text.trim() === ""))
        );
        if (cleanContent.length > 0) {
          this._messages.push({ role: "assistant", content: cleanContent });
        }

        // Extract and ingest inline <memory> blocks from the response
        this._extractAndIngestMemory(response.content);

        // Compress old tool results now that the LLM has processed them
        this._compressHistoryToolResults();

        // Show token usage + context pie
        this._emitContextUsage();
        this._trackAndEmitUsage(response);

        return;
      }

      // Append assistant response to history (filter empty text blocks)
      const cleanToolContent = response.content.filter(
        (b) => !(b.type === "text" && (!b.text || b.text.trim() === ""))
      );
      if (cleanToolContent.length > 0) {
        this._messages.push({
          role: "assistant",
          content: cleanToolContent,
        });
      }

      // Execute tool calls — parallel for safe tools, sequential for destructive
      const toolResults: ContentBlock[] = [];

      // Split into safe (parallel) and destructive (needs confirmation)
      const safeCalls = toolUses.filter((tu) => !DESTRUCTIVE_TOOLS.has(tu.name || ""));
      const destructiveCalls = toolUses.filter((tu) => DESTRUCTIVE_TOOLS.has(tu.name || ""));

      // Show spinner
      if (toolUses.length > 1) {
        this._emitter.fire({
          type: "spinner",
          text: `Running ${toolUses.length} tools...`,
          active: true,
        } as WebviewMessage);
      } else if (toolUses.length === 1) {
        this._emitter.fire({
          type: "spinner",
          text: this._toolSpinnerText(toolUses[0].name || "", toolUses[0].input || {}),
          active: true,
        } as WebviewMessage);
      }

      // Execute safe tools in parallel
      if (safeCalls.length > 0) {
        const safeResults = await Promise.all(
          safeCalls.map(async (tu) => {
            const toolName = tu.name || "unknown";
            const toolInput = tu.input || {};
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
            this._emitToolCall(toolName, toolInput, result);
            return { tu, result };
          })
        );

        for (const { tu, result } of safeResults) {
          toolResults.push({
            type: "tool_result",
            tool_use_id: tu.id,
            content: this._trimToolResult(result, tu.name || ""),
          });
        }
      }

      // Execute destructive tools sequentially (may need confirmation)
      for (const tu of destructiveCalls) {
        const toolName = tu.name || "unknown";
        const toolInput = tu.input || {};

        if (!this.autoEdit) {
          this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);

          const allowed = await this._requestConfirmation(toolName, toolInput);
          if (!allowed) {
            toolResults.push({
              type: "tool_result",
              tool_use_id: tu.id,
              content: `User denied: ${toolName} was not executed.`,
            });
            this._emitter.fire({ type: "agent", content: `Skipped ${toolName} (denied by user).` });
            continue;
          }

          this._emitter.fire({
            type: "spinner",
            text: this._toolSpinnerText(toolName, toolInput),
            active: true,
          } as WebviewMessage);
        }

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

        this._emitToolCall(toolName, toolInput, result);
        toolResults.push({
          type: "tool_result",
          tool_use_id: tu.id,
          content: this._trimToolResult(result, toolName),
        });
      }

      // Clear spinner
      this._emitter.fire({ type: "spinner", text: "", active: false } as WebviewMessage);

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

        // Log full error details for debugging
        const errBody = (err as any)?.error?.message || (err as any)?.response?.data || (err as any)?.body || "";
        if (status === 400) {
          throw new Error(`API error 400: ${msg}${errBody ? ` — ${JSON.stringify(errBody)}` : ""}`);
        }

        throw err;
      }
    }

    throw new Error("Rate limit: max retries exceeded. Try again in a minute.");
  }

  /** Streaming version of _callProvider — emits text_delta events as tokens arrive */
  private async _callProviderStreaming(
    onEvent: (event: import("../local/providers/types").StreamEvent) => void,
    enableThinking = true
  ): Promise<LLMResponse> {
    const systemPrompt = this._currentSystemPrompt || this._baseSystemPrompt;

    const provider = this._provider as {
      createMessageStreamWithModel?: (
        model: string,
        system: string,
        messages: ConversationMessage[],
        tools: ToolDefinition[],
        maxTokens: number,
        onEvent: (event: import("../local/providers/types").StreamEvent) => void,
        thinkingBudget?: number
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

    // Thinking budget: full on first call, 0 on tool-loop iterations
    const thinkingBudget = enableThinking ? (this._thinkingBudget || 4096) : 0;

    // Only use streaming for Anthropic — Gemini/OpenAI streaming has compatibility issues
    const isAnthropic = this._model.includes("claude");

    const call = () => {
      if (isAnthropic && provider.createMessageStreamWithModel) {
        return provider.createMessageStreamWithModel(
          this._model, systemPrompt, this._messages, TOOL_DEFINITIONS, this._maxTokens, onEvent, thinkingBudget
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
  /**
   * Trim tool results BEFORE sending to LLM (aggressive, like CC).
   * The full result is shown in the UI — this only affects what the model sees.
   */
  private _trimToolResult(result: string, toolName: string): string {
    // Short results pass through
    if (result.length <= 1500) return result;

    switch (toolName) {
      case "read": {
        // Keep first 40 + last 10 lines — model doesn't need the full file
        const lines = result.split("\n");
        if (lines.length > 60) {
          const head = lines.slice(0, 40).join("\n");
          const tail = lines.slice(-10).join("\n");
          return head + `\n\n... [${lines.length - 50} lines omitted] ...\n\n` + tail;
        }
        if (result.length > 3000) {
          return result.substring(0, 2500) + `\n... [truncated, ${lines.length} lines total]`;
        }
        return result;
      }

      case "grep":
      case "search": {
        // First 20 matches are enough for the model to understand
        const lines = result.split("\n");
        if (lines.length > 20) {
          return lines.slice(0, 20).join("\n") + `\n... [${lines.length - 20} more matches]`;
        }
        return result;
      }

      case "glob": {
        // File lists: first 30 entries
        const lines = result.split("\n");
        if (lines.length > 30) {
          return lines.slice(0, 30).join("\n") + `\n... [${lines.length - 30} more files]`;
        }
        return result;
      }

      case "bash": {
        // Command output: keep first 30 + last 10 lines
        const lines = result.split("\n");
        if (lines.length > 50) {
          const head = lines.slice(0, 30).join("\n");
          const tail = lines.slice(-10).join("\n");
          return head + `\n... [${lines.length - 40} lines omitted] ...\n` + tail;
        }
        if (result.length > 4000) {
          return result.substring(0, 3000) + `\n... [truncated]`;
        }
        return result;
      }

      case "edit": {
        // Edit results are already short summaries
        return result;
      }

      default: {
        if (result.length > 3000) {
          return result.substring(0, 2500) + `\n... [truncated, ${result.length} chars]`;
        }
        return result;
      }
    }
  }

  /**
   * Compress tool results in history AFTER the LLM has processed them.
   * Called after each successful LLM response that followed tool results.
   * Replaces verbose tool results with one-line summaries to save tokens
   * on all subsequent API calls.
   */
  private _compressHistoryToolResults(): void {
    for (let i = 0; i < this._messages.length; i++) {
      const msg = this._messages[i];
      if (msg.role !== "user" || typeof msg.content === "string") continue;

      const blocks = msg.content as ContentBlock[];
      let modified = false;

      for (let j = 0; j < blocks.length; j++) {
        const block = blocks[j];
        if (block.type !== "tool_result" || !block.content) continue;
        if (block.content.length <= 200) continue; // Already compact

        // Find the matching tool_use in the previous assistant message
        let toolName = "tool";
        if (i > 0) {
          const prev = this._messages[i - 1];
          if (prev.role === "assistant" && Array.isArray(prev.content)) {
            const toolUse = (prev.content as ContentBlock[]).find(
              (b) => b.type === "tool_use" && b.id === block.tool_use_id
            );
            if (toolUse) toolName = toolUse.name || "tool";
          }
        }

        // Compress based on tool type
        blocks[j] = {
          ...block,
          content: this._summarizeToolResult(toolName, block.content),
        };
        modified = true;
      }

      if (modified) {
        this._messages[i] = { ...msg, content: blocks };
      }
    }
  }

  /** Create a one-line summary of a tool result for history compression. */
  private _summarizeToolResult(toolName: string, result: string): string {
    const lines = result.split("\n");
    const lineCount = lines.length;
    const charCount = result.length;

    switch (toolName) {
      case "read":
        return `[Read: ${lineCount} lines, ${charCount} chars]`;
      case "write":
        // Keep the confirmation message as-is (already short)
        return result.length > 200 ? `[Wrote file: ${lineCount} lines]` : result;
      case "edit":
        return result; // Already short
      case "bash":
        // Keep first line (usually the most informative) + summary
        return lines[0] + (lineCount > 1 ? `\n... [${lineCount} lines total]` : "");
      case "glob":
        return `[Found ${lineCount} files]`;
      case "grep":
      case "search":
        return `[${lineCount} matches found]`;
      case "memory_search":
      case "memory_facts":
        return result.length > 300 ? result.substring(0, 200) + "..." : result;
      default:
        return result.length > 200 ? result.substring(0, 150) + `... [${charCount} chars]` : result;
    }
  }

  /** Emit context usage to the webview for the pie chart indicator. */
  /**
   * Extract <memory> JSON blocks from the LLM response and auto-ingest into MemGate.
   * The LLM includes these inline to avoid an extra tool call round-trip.
   */
  private _extractAndIngestMemory(content: ContentBlock[]): void {
    const MEMORY_RE = /<memory>\s*(\{[\s\S]*?\})\s*<\/memory>/g;

    for (const block of content) {
      if (block.type !== "text" || !block.text) continue;

      let match;
      while ((match = MEMORY_RE.exec(block.text)) !== null) {
        try {
          const payload = JSON.parse(match[1]);
          const memContent = payload.content;
          if (!memContent) continue;

          // Ingest asynchronously — don't block the response
          this._memoryBridge.store(
            memContent,
            payload.entities,
            payload.relationships
          ).catch(() => {
            // Silent failure — memory is best-effort
          });
        } catch {
          // Invalid JSON in <memory> block — skip
        }
      }
    }
  }

  /** Track cumulative token usage and emit to webview. */
  private _trackAndEmitUsage(response: LLMResponse): void {
    if (!response.usage) return;

    const u = response.usage;
    this._apiCalls++;
    this._totalInputTokens += u.inputTokens;
    this._totalOutputTokens += u.outputTokens;
    this._totalCacheReadTokens += u.cacheReadTokens || 0;
    this._totalCacheWriteTokens += u.cacheWriteTokens || 0;

    // Estimate cost (per million tokens)
    const cost = this._estimateCost(u.inputTokens, u.outputTokens, u.cacheReadTokens || 0, u.cacheWriteTokens || 0);
    const totalCost = this._estimateCost(this._totalInputTokens, this._totalOutputTokens, this._totalCacheReadTokens, this._totalCacheWriteTokens);

    this._emitter.fire({
      type: "token_usage",
      input: u.inputTokens,
      output: u.outputTokens,
      cacheRead: u.cacheReadTokens || 0,
      cacheWrite: u.cacheWriteTokens || 0,
      totalInput: this._totalInputTokens,
      totalOutput: this._totalOutputTokens,
      totalCacheRead: this._totalCacheReadTokens,
      totalCacheWrite: this._totalCacheWriteTokens,
      cost,
      totalCost,
      apiCalls: this._apiCalls,
    } as unknown as WebviewMessage);

    // Update status bar
    this._emitter.fire({
      type: "status",
      content: `Local (${this._model}) · $${totalCost.toFixed(4)}`,
    });
  }

  /** Estimate cost in USD based on model pricing. */
  private _estimateCost(input: number, output: number, cacheRead: number, cacheWrite: number): number {
    const m = this._model.toLowerCase();
    let inputPrice = 3; // $ per 1M tokens
    let outputPrice = 15;
    let cacheReadPrice = 0.3;
    let cacheWritePrice = 3.75;

    if (m.includes("opus")) {
      inputPrice = 15; outputPrice = 75; cacheReadPrice = 1.5; cacheWritePrice = 18.75;
    } else if (m.includes("sonnet")) {
      inputPrice = 3; outputPrice = 15; cacheReadPrice = 0.3; cacheWritePrice = 3.75;
    } else if (m.includes("haiku")) {
      inputPrice = 0.25; outputPrice = 1.25; cacheReadPrice = 0.025; cacheWritePrice = 0.3;
    } else if (m.includes("gpt-4o")) {
      inputPrice = 2.5; outputPrice = 10; cacheReadPrice = 1.25; cacheWritePrice = 2.5;
    } else if (m.includes("gpt-4")) {
      inputPrice = 30; outputPrice = 60; cacheReadPrice = 15; cacheWritePrice = 30;
    } else if (m.includes("gemini-2.5-pro")) {
      inputPrice = 1.25; outputPrice = 10; cacheReadPrice = 0.315; cacheWritePrice = 1.25;
    } else if (m.includes("gemini-2.5-flash")) {
      inputPrice = 0.15; outputPrice = 0.60; cacheReadPrice = 0.0375; cacheWritePrice = 0.15;
    } else if (m.includes("gemini-2.0-flash")) {
      inputPrice = 0.10; outputPrice = 0.40; cacheReadPrice = 0.025; cacheWritePrice = 0.10;
    }

    // input_tokens includes cache_read — subtract to avoid double-counting
    const nonCachedInput = Math.max(0, input - cacheRead);

    return (
      (nonCachedInput * inputPrice +
       cacheRead * cacheReadPrice +
       cacheWrite * cacheWritePrice +
       output * outputPrice) / 1_000_000
    );
  }

  private _log(msg: string): void {
    this._outputChannel?.appendLine(msg);
  }

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
