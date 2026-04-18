import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const NON_ACP_BLOCKING_FIELDS = ["streamTo", "resumeSessionId"];

function sanitizeSessionsSpawnParams(params) {
  const runtime = typeof params.runtime === "string" ? params.runtime : undefined;
  if (runtime === "acp") {
    return null;
  }

  let changed = false;
  const nextParams = { ...params };
  const removedFields = [];

  for (const field of NON_ACP_BLOCKING_FIELDS) {
    if (Object.prototype.hasOwnProperty.call(nextParams, field)) {
      delete nextParams[field];
      removedFields.push(field);
      changed = true;
    }
  }

  if (!changed) {
    return null;
  }

  return {
    runtime: runtime ?? "subagent(default)",
    params: nextParams,
    removedFields,
  };
}

export default definePluginEntry({
  id: "ashare-sessions-spawn-guard",
  name: "Ashare Sessions Spawn Guard",
  description: "Protect native subagent sessions_spawn calls from ACP-only parameters.",
  register(api) {
    api.on(
      "before_tool_call",
      (event, ctx) => {
        if (event.toolName !== "sessions_spawn") {
          return;
        }

        const sanitized = sanitizeSessionsSpawnParams(event.params ?? {});
        if (!sanitized) {
          return;
        }

        api.logger.warn(
          [
            "[ashare-sessions-spawn-guard] 已清洗非 ACP sessions_spawn 参数",
            `runtime=${sanitized.runtime}`,
            `removed=${sanitized.removedFields.join(",")}`,
            `agent=${ctx.agentId ?? "unknown"}`,
            `run=${ctx.runId ?? event.runId ?? "unknown"}`,
          ].join(" "),
        );

        return { params: sanitized.params };
      },
      {
        priority: 100,
      },
    );
  },
});
