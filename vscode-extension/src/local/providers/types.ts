/**
 * Provider-agnostic LLM interface.
 * Both Anthropic and OpenAI-compatible providers normalize into these types.
 */

export interface ContentBlock {
  type: "text" | "tool_use" | "tool_result";
  // text
  text?: string;
  // tool_use
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  // tool_result
  tool_use_id?: string;
  content?: string;
}

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string | ContentBlock[];
}

export interface ToolDefinition {
  name: string;
  description: string;
  input_schema: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
  };
}

export interface LLMResponse {
  content: ContentBlock[];
  stopReason: "end_turn" | "tool_use";
  usage?: {
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens?: number;
    cacheWriteTokens?: number;
  };
}

/** Events emitted during streaming */
export type StreamEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_use_start"; id: string; name: string }
  | { type: "tool_use_delta"; id: string; partialJson: string }
  | { type: "done"; response: LLMResponse };

export interface LLMProvider {
  createMessage(
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse>;

  /** Streaming version — calls onEvent for each token/block as it arrives */
  createMessageStream?(
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number,
    onEvent: (event: StreamEvent) => void
  ): Promise<LLMResponse>;
}

export interface ProviderConfig {
  provider: string;
  apiKey?: string;
  baseUrl?: string;
  model: string;
}

export function createProvider(config: ProviderConfig): LLMProvider {
  // Lazy imports to avoid loading both SDKs at startup
  switch (config.provider) {
    case "anthropic": {
      const { AnthropicProvider } = require("./anthropic");
      return new AnthropicProvider(config.apiKey!);
    }
    case "openai": {
      const { OpenAIProvider } = require("./openai");
      return new OpenAIProvider(config.apiKey, config.baseUrl);
    }
    case "gemini": {
      const { OpenAIProvider } = require("./openai");
      return new OpenAIProvider(
        config.apiKey,
        config.baseUrl || "https://generativelanguage.googleapis.com/v1beta/openai"
      );
    }
    case "dashscope":
    case "alibaba": {
      const { OpenAIProvider } = require("./openai");
      return new OpenAIProvider(
        config.apiKey,
        config.baseUrl || "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
      );
    }
    case "ollama": {
      const { OpenAIProvider } = require("./openai");
      return new OpenAIProvider(
        undefined,
        config.baseUrl || "http://localhost:11434/v1"
      );
    }
    default: {
      // Treat unknown providers as OpenAI-compatible
      const { OpenAIProvider } = require("./openai");
      return new OpenAIProvider(config.apiKey, config.baseUrl);
    }
  }
}
