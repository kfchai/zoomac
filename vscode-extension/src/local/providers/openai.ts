import OpenAI from "openai";
import type {
  ContentBlock,
  ConversationMessage,
  LLMProvider,
  LLMResponse,
  StreamEvent,
  ToolDefinition,
} from "./types";

/**
 * OpenAI-compatible provider.
 *
 * Works with: OpenAI, Ollama, vLLM, LM Studio, Together, Groq, etc.
 * Just change `baseURL` and optionally `apiKey`.
 *
 * Differences from Anthropic:
 * - `system` is a message with role "system"
 * - Tool results use a dedicated `tool` role with `tool_call_id`
 * - Tool call arguments come as a JSON string, not parsed object
 */
export class OpenAIProvider implements LLMProvider {
  private readonly _client: OpenAI;

  constructor(apiKey?: string, baseUrl?: string) {
    this._client = new OpenAI({
      apiKey: apiKey || "not-needed",
      baseURL: baseUrl,
    });
  }

  async createMessage(
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    const openaiMessages: OpenAI.ChatCompletionMessageParam[] = [
      { role: "system", content: system },
      ...this._convertMessages(messages),
    ];

    const params: OpenAI.ChatCompletionCreateParams = {
      model: "", // set by caller
      messages: openaiMessages,
      max_tokens: maxTokens,
    };

    if (tools.length > 0) {
      params.tools = tools.map((t) => ({
        type: "function" as const,
        function: {
          name: t.name,
          description: t.description,
          parameters: t.input_schema,
        },
      }));
    }

    let response;
    try {
      response = await this._client.chat.completions.create(params);
    } catch (err: unknown) {
      const e = err as any;
      const detail = e?.error?.message || e?.message || e?.body || String(err);
      throw new Error(`API error: ${detail}`);
    }
    const choice = response.choices[0];

    if (!choice) {
      return { content: [], stopReason: "end_turn" };
    }

    const content: ContentBlock[] = [];

    // Text content
    if (choice.message.content) {
      content.push({ type: "text", text: choice.message.content });
    }

    // Tool calls
    if (choice.message.tool_calls) {
      for (const tc of choice.message.tool_calls) {
        let input: Record<string, unknown> = {};
        try {
          input = JSON.parse(tc.function.arguments);
        } catch {
          input = { raw: tc.function.arguments };
        }
        content.push({
          type: "tool_use",
          id: tc.id,
          name: tc.function.name,
          input,
        });
      }
    }

    const hasToolCalls =
      choice.finish_reason === "tool_calls" ||
      (choice.message.tool_calls && choice.message.tool_calls.length > 0);

    return {
      content,
      stopReason: hasToolCalls ? "tool_use" : "end_turn",
      usage: response.usage
        ? {
            inputTokens: response.usage.prompt_tokens,
            outputTokens: response.usage.completion_tokens,
          }
        : undefined,
    };
  }

  /** With model injected */
  createMessageWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    return this._createWithModel(model, system, messages, tools, maxTokens);
  }

  private _textToolMode = false; // Auto-detected: true = tools in prompt, parse from text

  private async _createWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    const converted = this._convertMessages(messages);

    // Build system prompt — embed tools as text for models without native tool calling
    let effectiveSystem = system;
    if (this._textToolMode && tools.length > 0) {
      effectiveSystem = system + "\n\n" + this._buildTextToolPrompt(tools);
    }

    const openaiMessages: OpenAI.ChatCompletionMessageParam[] = [
      { role: "system", content: effectiveSystem },
      ...converted,
    ];

    const params: OpenAI.ChatCompletionCreateParams = {
      model,
      messages: openaiMessages,
      max_tokens: maxTokens,
    };

    // Only pass tools via API if not in text-tool mode
    if (!this._textToolMode && tools.length > 0) {
      params.tools = tools.map((t) => ({
        type: "function" as const,
        function: {
          name: t.name,
          description: t.description,
          parameters: t.input_schema,
        },
      }));
    }

    let response;
    try {
      response = await this._client.chat.completions.create(params);
    } catch (err: unknown) {
      const e = err as any;
      const detail = e?.error?.message || e?.message || e?.body || String(err);

      // If model doesn't support tools, switch to text-tool mode and retry
      if (detail.includes("does not support tools") || detail.includes("tool_use is not supported")) {
        this._textToolMode = true;
        return this._createWithModel(model, system, messages, tools, maxTokens);
      }

      const roles = converted.map((m: any) => `${m.role}${m.tool_call_id ? "(tool)" : ""}${m.tool_calls ? `(${m.tool_calls.length}calls)` : ""}`).join("→");
      throw new Error(`API error: ${detail} | msg flow: [sys]→${roles}`);
    }
    const choice = response.choices[0];

    if (!choice) {
      return { content: [], stopReason: "end_turn" };
    }

    const content: ContentBlock[] = [];
    let hasToolCalls = false;

    if (choice.message.tool_calls && choice.message.tool_calls.length > 0) {
      // Native tool calling worked
      if (choice.message.content) {
        content.push({ type: "text", text: choice.message.content });
      }
      for (const tc of choice.message.tool_calls) {
        let input: Record<string, unknown> = {};
        try {
          input = JSON.parse(tc.function.arguments);
        } catch {
          input = { raw: tc.function.arguments };
        }
        content.push({
          type: "tool_use",
          id: tc.id,
          name: tc.function.name,
          input,
        });
      }
      hasToolCalls = true;
    } else if (choice.message.content) {
      // Check for text-based tool calls: <tool_call>{"name":"...","arguments":{...}}</tool_call>
      const textToolCalls = this._parseTextToolCalls(choice.message.content);

      if (textToolCalls.length > 0) {
        // Extract non-tool-call text
        const cleanText = choice.message.content
          .replace(/<tool_call>[\s\S]*?<\/tool_call>/g, "")
          .trim();
        if (cleanText) {
          content.push({ type: "text", text: cleanText });
        }
        for (const tc of textToolCalls) {
          content.push(tc);
        }
        hasToolCalls = true;

        // Model used text tool calls — enable text-tool mode for future calls
        if (!this._textToolMode) {
          this._textToolMode = true;
        }
      } else if (!this._textToolMode && tools.length > 0 && !choice.message.content.includes("```")) {
        // First call returned no tool calls at all — switch to text-tool mode
        // (the model likely doesn't support native tool calling)
        this._textToolMode = true;
        // Re-try with tools in the system prompt
        return this._createWithModel(model, system, messages, tools, maxTokens);
      } else {
        content.push({ type: "text", text: choice.message.content });
      }
    }

    hasToolCalls = hasToolCalls ||
      choice.finish_reason === "tool_calls";

    return {
      content,
      stopReason: hasToolCalls ? "tool_use" : "end_turn",
      usage: response.usage
        ? {
            inputTokens: response.usage.prompt_tokens,
            outputTokens: response.usage.completion_tokens,
          }
        : undefined,
    };
  }

  async createMessageStreamWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number,
    onEvent: (event: StreamEvent) => void
  ): Promise<LLMResponse> {
    const openaiMessages: OpenAI.ChatCompletionMessageParam[] = [
      { role: "system", content: system },
      ...this._convertMessages(messages),
    ];

    const params: OpenAI.ChatCompletionCreateParams = {
      model,
      messages: openaiMessages,
      max_tokens: maxTokens,
      stream: true,
    };

    if (tools.length > 0) {
      params.tools = tools.map((t) => ({
        type: "function" as const,
        function: {
          name: t.name,
          description: t.description,
          parameters: t.input_schema,
        },
      }));
    }

    let stream;
    try {
      stream = await this._client.chat.completions.create(params);
    } catch (err: unknown) {
      const e = err as any;
      // Extract error details from multiple possible locations
      const detail = e?.error?.message
        || e?.message
        || e?.body?.error?.message
        || (typeof e?.body === "string" ? e.body : "")
        || (e?.body ? JSON.stringify(e.body) : "")
        || (e?.response?.data ? JSON.stringify(e.response.data) : "")
        || `${e?.status || ""} ${e?.statusText || ""}`.trim()
        || String(err);
      throw new Error(`API error (${e?.status || "?"}): ${detail}`);
    }

    // Accumulate content
    let textContent = "";
    const toolCalls = new Map<number, { id: string; name: string; args: string }>();

    for await (const chunk of stream as AsyncIterable<OpenAI.ChatCompletionChunk>) {
      const delta = chunk.choices[0]?.delta;
      if (!delta) continue;

      // Text delta
      if (delta.content) {
        textContent += delta.content;
        onEvent({ type: "text_delta", text: delta.content });
      }

      // Tool call deltas
      if (delta.tool_calls) {
        for (const tc of delta.tool_calls) {
          const idx = tc.index;
          if (!toolCalls.has(idx)) {
            toolCalls.set(idx, { id: tc.id || "", name: tc.function?.name || "", args: "" });
            if (tc.id && tc.function?.name) {
              onEvent({ type: "tool_use_start", id: tc.id, name: tc.function.name });
            }
          }
          const entry = toolCalls.get(idx)!;
          if (tc.id) entry.id = tc.id;
          if (tc.function?.name) entry.name = tc.function.name;
          if (tc.function?.arguments) {
            entry.args += tc.function.arguments;
            onEvent({ type: "tool_use_delta", id: entry.id, partialJson: tc.function.arguments });
          }
        }
      }
    }

    // Build final response
    const content: ContentBlock[] = [];
    if (textContent) {
      content.push({ type: "text", text: textContent });
    }
    for (const [, tc] of toolCalls) {
      let input: Record<string, unknown> = {};
      try {
        input = JSON.parse(tc.args);
      } catch {
        input = { raw: tc.args };
      }
      content.push({ type: "tool_use", id: tc.id, name: tc.name, input });
    }

    const hasToolCalls = toolCalls.size > 0;
    const response: LLMResponse = {
      content,
      stopReason: hasToolCalls ? "tool_use" : "end_turn",
    };

    onEvent({ type: "done", response });
    return response;
  }

  /**
   * Convert messages for Gemini/OpenAI compatibility.
   * Fixes: empty content, tool_result splitting, null content on assistant messages.
   */
  private _convertMessages(messages: ConversationMessage[]): OpenAI.ChatCompletionMessageParam[] {
    const result: OpenAI.ChatCompletionMessageParam[] = [];

    // Build a map of tool_use_id → tool name for Gemini compatibility
    const toolNameMap = new Map<string, string>();
    for (const msg of messages) {
      if (typeof msg.content !== "string" && Array.isArray(msg.content)) {
        for (const b of msg.content) {
          if (b.type === "tool_use" && b.id && b.name) {
            toolNameMap.set(b.id, b.name);
          }
        }
      }
    }

    for (const msg of messages) {
      if (!msg) continue;

      // Skip empty
      if (!msg.content) continue;
      if (Array.isArray(msg.content) && msg.content.length === 0) continue;

      if (typeof msg.content === "string") {
        if (!msg.content && msg.role === "user") continue;
        result.push({ role: msg.role, content: msg.content || "" });
        continue;
      }

      // Handle tool_result blocks — split into individual "tool" messages
      const toolResults = msg.content.filter((b) => b.type === "tool_result");
      if (toolResults.length > 0) {
        for (const tr of toolResults) {
          if (tr.tool_use_id) {
            result.push({
              role: "tool" as const,
              tool_call_id: tr.tool_use_id,
              content: tr.content || "(no output)",
            });
          }
        }
        continue;
      }

      // Handle assistant messages with tool_use blocks
      const toolCalls = msg.content.filter((b) => b.type === "tool_use");
      if (toolCalls.length > 0) {
        const textParts = msg.content
          .filter((b) => b.type === "text" && b.text)
          .map((b) => b.text)
          .join("");

        result.push({
          role: "assistant",
          // CRITICAL: Gemini rejects null content — use empty string
          content: textParts || "",
          tool_calls: toolCalls.map((tc) => ({
            id: tc.id || "call_" + Math.random().toString(36).substring(2, 8),
            type: "function" as const,
            function: {
              name: tc.name || "unknown",
              arguments: JSON.stringify(tc.input || {}),
            },
          })),
        });
        continue;
      }

      // Plain text
      const text = msg.content
        .filter((b) => b.type === "text" && b.text)
        .map((b) => b.text)
        .join("");
      if (text) {
        result.push({ role: msg.role, content: text });
      }
    }

    // Enforce user/assistant alternation — merge consecutive same-role messages
    // Gemini rejects non-alternating sequences
    return this._enforceAlternation(result);
  }

  /** Build tool definitions as text for the system prompt (fallback for models without native tool calling). */
  private _buildTextToolPrompt(tools: ToolDefinition[]): string {
    let prompt = "## Available Tools\n\n";
    prompt += "When you want to use a tool, output a tool_call block in your response:\n\n";
    prompt += "```\n<tool_call>\n{\"name\": \"tool_name\", \"arguments\": {\"param\": \"value\"}}\n</tool_call>\n```\n\n";
    prompt += "You can call multiple tools in one response. After each tool call, you will receive the result.\n\n";
    prompt += "Tools:\n\n";

    for (const t of tools) {
      const params = t.input_schema.properties
        ? Object.entries(t.input_schema.properties as Record<string, any>)
            .map(([k, v]) => `  - ${k} (${v.type || "string"}${(t.input_schema.required || []).includes(k) ? ", required" : ""}): ${v.description || ""}`)
            .join("\n")
        : "  (no parameters)";
      prompt += `### ${t.name}\n${t.description}\nParameters:\n${params}\n\n`;
    }

    return prompt;
  }

  /** Parse <tool_call> blocks from model text output. */
  private _parseTextToolCalls(text: string): ContentBlock[] {
    const results: ContentBlock[] = [];
    const re = /<tool_call>\s*([\s\S]*?)\s*<\/tool_call>/g;
    let match;
    let counter = 0;

    while ((match = re.exec(text)) !== null) {
      try {
        const parsed = JSON.parse(match[1]);
        const name = parsed.name || parsed.function || "";
        const args = parsed.arguments || parsed.params || parsed.input || {};
        if (name) {
          results.push({
            type: "tool_use",
            id: "text_tc_" + (++counter),
            name,
            input: typeof args === "string" ? JSON.parse(args) : args,
          });
        }
      } catch {
        // Invalid JSON in tool_call block — skip
      }
    }

    return results;
  }

  /** Merge consecutive same-role messages to enforce user/assistant alternation. */
  private _enforceAlternation(messages: OpenAI.ChatCompletionMessageParam[]): OpenAI.ChatCompletionMessageParam[] {
    if (messages.length === 0) return messages;

    const merged: OpenAI.ChatCompletionMessageParam[] = [];

    for (const msg of messages) {
      const prev = merged.length > 0 ? merged[merged.length - 1] : null;

      // "tool" role messages are fine after "assistant" — don't merge those
      if (msg.role === "tool") {
        merged.push(msg);
        continue;
      }

      // Merge consecutive user messages
      if (prev && prev.role === "user" && msg.role === "user") {
        const prevContent = typeof prev.content === "string" ? prev.content : "";
        const msgContent = typeof msg.content === "string" ? msg.content : "";
        (prev as any).content = (prevContent + "\n\n" + msgContent).trim();
        continue;
      }

      // Merge consecutive assistant messages (without tool_calls)
      if (prev && prev.role === "assistant" && msg.role === "assistant"
          && !(prev as any).tool_calls?.length && !(msg as any).tool_calls?.length) {
        const prevContent = typeof prev.content === "string" ? prev.content : "";
        const msgContent = typeof msg.content === "string" ? msg.content : "";
        (prev as any).content = (prevContent + "\n\n" + msgContent).trim();
        continue;
      }

      // If we'd have user→user with a tool message in between, insert a placeholder assistant
      if (prev && prev.role !== "assistant" && prev.role !== "tool" && msg.role === "user") {
        // Only if prev is also "user" — already handled above by merge
      }

      merged.push(msg);
    }

    return merged;
  }
}
