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

  private async _createWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    const converted = this._convertMessages(messages);
    const openaiMessages: OpenAI.ChatCompletionMessageParam[] = [
      { role: "system", content: system },
      ...converted,
    ];

    // Debug: log messages being sent
    for (const m of converted) {
      const preview = typeof m.content === "string" ? m.content?.substring(0, 60) : JSON.stringify(m.content)?.substring(0, 60);
      console.log(`[openai] msg: role=${m.role}, content=${preview}, tool_calls=${(m as any).tool_calls?.length || 0}, tool_call_id=${(m as any).tool_call_id || ""}`);
    }

    const params: OpenAI.ChatCompletionCreateParams = {
      model,
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
      const roles = converted.map((m: any) => `${m.role}${m.tool_call_id ? "(tool)" : ""}${m.tool_calls ? `(${m.tool_calls.length}calls)` : ""}`).join("→");
      throw new Error(`API error: ${detail} | msg flow: [sys]→${roles}`);
    }
    const choice = response.choices[0];

    if (!choice) {
      return { content: [], stopReason: "end_turn" };
    }

    const content: ContentBlock[] = [];

    if (choice.message.content) {
      content.push({ type: "text", text: choice.message.content });
    }

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

    return result;
  }
}
