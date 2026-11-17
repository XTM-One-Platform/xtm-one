// SPDX-License-Identifier: Apache-2.0

/**
 * What deleting a provider config does, in words.
 *
 * `GET /settings/llm-providers/{id}/deletion-impact` says, per route, what it
 * becomes; the delete applies exactly that. These helpers turn the answer into
 * the lines the confirmation reads out - each an i18n message id with its
 * values, so the dialog translates and this stays testable under `node --test`.
 */
import type { LLMProviderDeletionImpact, ProviderDeletionRoute, ProviderRouteTarget } from "@/types";

export type DeletionLineTone = "info" | "warning" | "danger";

export interface DeletionLine {
  /** Stable React key. */
  key: string;
  tone: DeletionLineTone;
  /** i18n message id (the English text). */
  message: string;
  values?: Record<string, string | number>;
  /** Names of the agents / users concerned, already bounded by the backend. */
  names?: string[];
  /** How many more are concerned than `names` lists. */
  more?: number;
}

/** Names a provider config the way the Settings list does. */
type ProviderNamer = (target: ProviderRouteTarget) => string;

/**
 * Whether the admin has a replacement to choose: a route actually moves to it
 * and a survivor can take it. A route that is only cleared (a stale provider
 * name with no model) ignores the choice, so it never justifies the selector.
 */
export function deletionNeedsReplacement(impact: LLMProviderDeletionImpact): boolean {
  if (impact.candidates.length === 0) return false;
  return [impact.agent_default, impact.general_assistant, impact.image_default].some(
    (route) => route.action === "reseeded",
  );
}

function changesSomething(impact: LLMProviderDeletionImpact): boolean {
  const routes: ProviderDeletionRoute[] = [
    impact.agent_default,
    impact.general_assistant,
    impact.image_default,
    impact.embedding_default,
    impact.routing_model,
    impact.fallback_model,
    impact.decision_model,
  ];
  return (
    routes.some((route) => route.affected) ||
    impact.agents.count > 0 ||
    impact.users.count > 0 ||
    Boolean(impact.implicit_chat_moves)
  );
}

interface ReseedMessages {
  /** The route keeps its model: the replacement offers it. */
  kept: string;
  /** The route moves to the replacement's default model, which differs. */
  moved: string;
  /** The route was pinned with no model and stays so. */
  withoutModel?: string;
}

function reseedLine(
  key: string,
  route: ProviderDeletionRoute,
  nameOf: ProviderNamer,
  messages: ReseedMessages,
): DeletionLine | null {
  if (route.action !== "reseeded" || !route.after) return null;
  const provider = nameOf(route.after);
  const model = route.after.model;
  if (!model) {
    return messages.withoutModel
      ? { key, tone: "info", message: messages.withoutModel, values: { provider } }
      : null;
  }
  if (route.model_kept) {
    return { key, tone: "info", message: messages.kept, values: { model, provider } };
  }
  return {
    key,
    tone: "info",
    message: messages.moved,
    values: { model, provider, previous: route.before?.model ?? "" },
  };
}

interface DescribeOptions {
  /**
   * Whether agentic AI runs on this install. On Community Edition nothing
   * runs until Enterprise Edition is active, so the lines say what the
   * deletion changes in the configuration and never that something "stops
   * until one is configured" (invariant 17).
   */
  licensed?: boolean;
}

/** The lines the confirmation shows, most consequential first. */
export function describeProviderDeletion(
  impact: LLMProviderDeletionImpact,
  nameOf: ProviderNamer,
  { licensed = true }: DescribeOptions = {},
): DeletionLine[] {
  const lines: DeletionLine[] = [];
  const push = (line: DeletionLine | null) => {
    if (line) lines.push(line);
  };
  const chatStops = licensed && !impact.chat_available;

  const landing = impact.implicit_chat_fallback;
  if (chatStops && landing) {
    // No remaining provider lists a chat model, yet the router still sends
    // chat to one on its provider's built-in model: say where, not "stops".
    push({
      key: "no-chat",
      tone: "warning",
      message:
        "No remaining provider lists a chat model: chat falls back to {provider} on its built-in model {model}, which it may not be able to serve.",
      values: { provider: nameOf(landing), model: landing.model ?? "" },
    });
  } else if (chatStops) {
    push({
      key: "no-chat",
      tone: "danger",
      message:
        "No remaining provider offers a chat model, so chat, agents and assignments stop working until one is configured.",
    });
  }

  push(
    reseedLine("agent-default", impact.agent_default, nameOf, {
      kept: "The platform default model keeps {model}, now on {provider}.",
      moved:
        "The platform default model becomes {model}, the default model of {provider}, which does not list {previous}.",
      withoutModel: "The platform default moves to {provider}, on its default model.",
    }),
  );
  push(
    reseedLine("general-assistant", impact.general_assistant, nameOf, {
      kept: "The General Assistant chat model keeps {model}, now on {provider}.",
      moved:
        "The General Assistant chat model becomes {model}, the default model of {provider}, which does not list {previous}.",
      withoutModel: "The General Assistant moves to {provider}, on its default model.",
    }),
  );
  // With no chat provider left, the danger line already says what a cleared
  // chat default means.
  if (!chatStops && impact.agent_default.action === "cleared") {
    push({ key: "agent-default", tone: "info", message: "The platform default model is cleared." });
  }
  if (!chatStops && impact.general_assistant.action === "cleared") {
    push({
      key: "general-assistant",
      tone: "info",
      message: "The General Assistant chat model is cleared: it follows the platform default model.",
    });
  }

  // No chat default is stored and chat goes to this provider today: the move
  // changes nothing stored, so say it (the no-chat line already names the
  // landing when no remaining provider lists a chat model).
  if (impact.implicit_chat_moves && landing && !chatStops) {
    push({
      key: "implicit-chat",
      tone: "info",
      message:
        "No default chat model is set, so chat currently goes to this provider. It will go to {provider} on {model}.",
      values: { provider: nameOf(landing), model: landing.model ?? "" },
    });
  }

  const unlisted = impact.unlisted ?? [];
  if (unlisted.length > 0) {
    push({
      key: "unlisted",
      tone: "info",
      message:
        "{count, plural, one {# remaining provider is} other {# remaining providers are}} not offered as the replacement: a provider that lists no model cannot take over a default that runs on one.",
      values: { count: unlisted.length },
      names: unlisted.map(nameOf),
    });
  }

  if (impact.agents.count > 0) {
    push({
      key: "agents",
      tone: "info",
      message:
        "{count, plural, one {# agent uses} other {# agents use}} this provider directly and will follow the platform default model instead.",
      values: { count: impact.agents.count },
      names: impact.agents.names,
      more: Math.max(0, impact.agents.count - impact.agents.names.length),
    });
  }
  if (impact.users.count > 0) {
    push({
      key: "users",
      tone: "info",
      message:
        "{count, plural, one {# user prefers} other {# users prefer}} a model on this provider; {count, plural, one {that preference is} other {those preferences are}} cleared.",
      values: { count: impact.users.count },
      names: impact.users.names,
      more: Math.max(0, impact.users.count - impact.users.names.length),
    });
  }

  const image = impact.image_default;
  if (image.action === "reseeded") {
    push(
      reseedLine("image", image, nameOf, {
        kept: "Image generation keeps {model}, now on {provider}.",
        moved:
          "Image generation moves to {model}, the image model of {provider}, because no remaining provider offers {previous}.",
      }),
    );
  } else if (image.action === "cleared") {
    push(
      licensed && image.before?.model
        ? {
            key: "image",
            tone: "warning",
            message:
              "The image default is cleared: no remaining provider has an image model, so image generation stops until one is configured.",
          }
        : { key: "image", tone: "info", message: "The image default is cleared." },
    );
  }

  const embedding = impact.embedding_default;
  if (embedding.action === "cleared") {
    const previous = embedding.before?.model;
    if (!licensed) {
      push({ key: "embedding", tone: "info", message: "The embedding default is cleared." });
    } else if (!embedding.fallback) {
      push({
        key: "embedding",
        tone: "warning",
        message:
          "The embedding default is cleared and no remaining provider can compute embeddings: knowledge base search and document ingestion stop until one is configured.",
      });
    } else if (embedding.fallback_same_model) {
      push({
        key: "embedding",
        tone: "info",
        message:
          "The embedding default is cleared. Embeddings fall back to the same model, {model} on {provider}, so knowledge base search keeps working; set an embedding default to pin it.",
        values: { model: embedding.fallback.model ?? "", provider: nameOf(embedding.fallback) },
      });
    } else {
      push({
        key: "embedding",
        tone: "warning",
        message: previous
          ? "The embedding default is cleared and not replaced automatically, because another embedding model cannot search the vectors already stored. Until you set one, embeddings fall back to {model} on {provider}, which cannot match content indexed with {previous}."
          : "The embedding default is cleared and not replaced automatically. Until you set one, embeddings fall back to {model} on {provider}.",
        values: {
          model: embedding.fallback.model ?? "",
          provider: nameOf(embedding.fallback),
          ...(previous ? { previous } : {}),
        },
      });
      if (embedding.knowledge_base_count > 0) {
        push({
          key: "embedding-reindex",
          tone: "warning",
          message:
            "Setting a new embedding default re-embeds {count, plural, one {the knowledge base that holds} other {the # knowledge bases that hold}} indexed content.",
          values: { count: embedding.knowledge_base_count },
        });
      }
    }
  }

  // No embedding default stored, and embeddings fall back to this provider
  // today: nothing stored changes, but where embeddings go does.
  if (embedding.action === "unchanged" && embedding.fallback_moves) {
    const previous = embedding.previous_fallback?.model ?? "";
    if (!licensed) {
      push({ key: "embedding", tone: "info", message: "Embeddings no longer fall back to this provider." });
    } else if (!embedding.fallback) {
      push({
        key: "embedding",
        tone: "warning",
        message:
          "Embeddings fall back to this provider today, and no remaining provider can compute them: knowledge base search and document ingestion stop until one is configured.",
      });
    } else if (embedding.fallback_same_model) {
      push({
        key: "embedding",
        tone: "info",
        message:
          "Embeddings fall back to this provider today; they move to the same model, {model} on {provider}, so knowledge base search keeps working.",
        values: { model: embedding.fallback.model ?? "", provider: nameOf(embedding.fallback) },
      });
    } else {
      push({
        key: "embedding",
        tone: "warning",
        message:
          "Embeddings fall back to this provider today, on {previous}. They move to {model} on {provider}, which cannot match content indexed with {previous}; set an embedding default to choose one.",
        values: { model: embedding.fallback.model ?? "", provider: nameOf(embedding.fallback), previous },
      });
      if (embedding.knowledge_base_count > 0) {
        push({
          key: "embedding-reindex",
          tone: "warning",
          message:
            "Setting a new embedding default re-embeds {count, plural, one {the knowledge base that holds} other {the # knowledge bases that hold}} indexed content.",
          values: { count: embedding.knowledge_base_count },
        });
      }
    }
  }

  if (impact.routing_model.affected) {
    push({
      key: "routing",
      tone: "info",
      message: "The routing model is cleared: routing decisions use the platform default model.",
    });
  }
  if (impact.fallback_model.affected) {
    push({
      key: "fallback",
      tone: "info",
      message: "The fallback model is cleared: failover uses the other providers' default models.",
    });
  }
  if (impact.decision_model.affected) {
    push({
      key: "decision",
      tone: "info",
      message: "The decision model is cleared: agents can no longer act on the pages of embedded chats until another is set.",
    });
  }

  const envManaged = [
    impact.agent_default,
    impact.general_assistant,
    impact.image_default,
    impact.embedding_default,
    impact.routing_model,
    impact.fallback_model,
  ].some((route) => route.affected && route.env_managed);
  if (envManaged) {
    push({
      key: "env",
      tone: "info",
      message:
        "Some of these defaults are set by environment variables: the environment values come back at the next restart.",
    });
  }

  if (!changesSomething(impact)) {
    push({
      key: "nothing",
      tone: "info",
      message: "No default, agent or user preference uses this provider.",
    });
  }
  return lines;
}
