import type { ToolDefinition } from "./providers/types";

/** Tool definitions in the neutral format used by both providers. */
export const TOOL_DEFINITIONS: ToolDefinition[] = [
  {
    name: "read",
    description:
      "Read a file from the workspace. Returns file contents with line numbers. " +
      "Use offset and limit for large files.",
    input_schema: {
      type: "object",
      properties: {
        file_path: {
          type: "string",
          description: "Absolute or workspace-relative file path",
        },
        offset: {
          type: "number",
          description: "Start reading from this line number (0-indexed)",
        },
        limit: {
          type: "number",
          description: "Maximum number of lines to read",
        },
      },
      required: ["file_path"],
    },
  },
  {
    name: "write",
    description:
      "Write content to a file. Creates the file and parent directories if they don't exist. " +
      "Overwrites existing content.",
    input_schema: {
      type: "object",
      properties: {
        file_path: {
          type: "string",
          description: "Absolute or workspace-relative file path",
        },
        content: {
          type: "string",
          description: "Full file content to write",
        },
      },
      required: ["file_path", "content"],
    },
  },
  {
    name: "edit",
    description:
      "Replace a specific string in a file. The old_string must be unique in the file. " +
      "Read the file first before editing.",
    input_schema: {
      type: "object",
      properties: {
        file_path: {
          type: "string",
          description: "Absolute or workspace-relative file path",
        },
        old_string: {
          type: "string",
          description: "The exact text to find and replace (must be unique in the file)",
        },
        new_string: {
          type: "string",
          description: "The replacement text",
        },
      },
      required: ["file_path", "old_string", "new_string"],
    },
  },
  {
    name: "bash",
    description:
      "Execute a shell command in the workspace directory. " +
      "Returns stdout and stderr. Use for running tests, git commands, builds, etc.",
    input_schema: {
      type: "object",
      properties: {
        command: {
          type: "string",
          description: "Shell command to execute",
        },
        timeout: {
          type: "number",
          description: "Timeout in milliseconds (default: 30000)",
        },
      },
      required: ["command"],
    },
  },
  {
    name: "glob",
    description:
      "Find files matching a glob pattern in the workspace. " +
      'Returns matching file paths. Example: "**/*.ts", "src/**/*.py".',
    input_schema: {
      type: "object",
      properties: {
        pattern: {
          type: "string",
          description: "Glob pattern to match files",
        },
        path: {
          type: "string",
          description: "Directory to search in (default: workspace root)",
        },
      },
      required: ["pattern"],
    },
  },
  {
    name: "grep",
    description:
      "Search file contents for a regex pattern. " +
      "Returns matching lines with file paths and line numbers.",
    input_schema: {
      type: "object",
      properties: {
        pattern: {
          type: "string",
          description: "Regex pattern to search for",
        },
        path: {
          type: "string",
          description: "File or directory to search in (default: workspace root)",
        },
        glob: {
          type: "string",
          description: 'File pattern filter (e.g., "*.ts", "*.py")',
        },
      },
      required: ["pattern"],
    },
  },
  // ── Memory tools ──
  {
    name: "memory_search",
    description:
      "Search long-term memory for relevant information. Use when you need to recall " +
      "facts about the user, project conventions, past decisions, or previously discussed topics.",
    input_schema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "Natural language search query",
        },
      },
      required: ["query"],
    },
  },
  {
    name: "memory_store",
    description:
      "Store important information in long-term memory for future recall. " +
      "Use when you learn something worth remembering: user preferences, project conventions, " +
      "key decisions, entity facts, or corrections. Do NOT store transient task details.",
    input_schema: {
      type: "object",
      properties: {
        content: {
          type: "string",
          description: "Concise summary of what to remember",
        },
        entities: {
          type: "array",
          description: "Structured entity facts to store",
          items: {
            type: "object",
            properties: {
              name: { type: "string", description: "Entity name" },
              attribute: { type: "string", description: "Property name" },
              value: { type: "string", description: "Property value" },
            },
            required: ["name", "attribute", "value"],
          },
        },
        relationships: {
          type: "array",
          description: "Relationships between entities",
          items: {
            type: "object",
            properties: {
              a: { type: "string", description: "First entity" },
              relation: { type: "string", description: "Relationship type" },
              b: { type: "string", description: "Second entity" },
            },
            required: ["a", "relation", "b"],
          },
        },
      },
      required: ["content"],
    },
  },
  {
    name: "memory_facts",
    description:
      "Look up known facts about a specific entity from long-term memory. " +
      "Use when you need structured information about a person, project, tool, or concept.",
    input_schema: {
      type: "object",
      properties: {
        entity: {
          type: "string",
          description: "Entity name to look up (e.g., 'user', 'project', 'postgres')",
        },
      },
      required: ["entity"],
    },
  },
];
