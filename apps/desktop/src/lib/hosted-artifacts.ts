import {
  hostedArtifactFrameOrigin,
  type HostedArtifactFramePolicy,
  isHostedArtifactId
} from '../../electron/hosted-artifact-frame'

export type { HostedArtifactFramePolicy }

/**
 * Server-hosted artifacts as they reach the transcript: the result text of
 * the artifacts MCP server's tools. The desktop has no MCP connection of its
 * own; the tool result is the whole contract.
 *
 * create / update / show answer `{id, version, viewer_url, content_url}`
 * (show adds `title`). `content_url` carries a short-lived view ticket for the
 * artifact's own origin; `viewer_url` is the durable page that always works.
 */

/** Registry prefix of the artifacts server's tools (`mcp__<server>__<tool>`). */
export const HOSTED_ARTIFACT_TOOL_PREFIX = 'mcp__hermes_artifacts__'

/** The tools whose result names one renderable artifact version. */
const RENDERED_TOOLS = new Set(['artifact_create', 'artifact_update', 'artifact_show'])

/** A view ticket's lifetime. A result older than this cannot open its frame
 *  any more (the content host answers its not-found page), so it links. */
export const HOSTED_ARTIFACT_TICKET_TTL_SECONDS = 300

export function isHostedArtifactCardTool(toolName: string | undefined): boolean {
  return (
    typeof toolName === 'string' &&
    toolName.startsWith(HOSTED_ARTIFACT_TOOL_PREFIX) &&
    RENDERED_TOOLS.has(toolName.slice(HOSTED_ARTIFACT_TOOL_PREFIX.length))
  )
}

export interface HostedArtifactResult {
  contentUrl: string
  id: string
  title: string
  version: number
  viewerUrl: string
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

function parseJsonObject(value: unknown): Record<string, unknown> | null {
  if (isRecord(value)) {
    return value
  }

  if (typeof value !== 'string' || !value.trim()) {
    return null
  }

  try {
    const parsed: unknown = JSON.parse(value)

    return isRecord(parsed) ? parsed : null
  } catch {
    return null
  }
}

function asWebUrl(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null
  }

  try {
    const url = new URL(value.trim())

    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : null
  } catch {
    return null
  }
}

function fromPayload(payload: Record<string, unknown>): HostedArtifactResult | null {
  const { id, version } = payload
  const viewerUrl = asWebUrl(payload.viewer_url)

  if (
    !isHostedArtifactId(id) ||
    typeof version !== 'number' ||
    !Number.isInteger(version) ||
    version < 1 ||
    !viewerUrl
  ) {
    return null
  }

  return {
    // Validated for framing separately; kept verbatim so the ticket survives.
    contentUrl: typeof payload.content_url === 'string' ? payload.content_url.trim() : '',
    id,
    title: typeof payload.title === 'string' ? payload.title.trim() : '',
    version,
    viewerUrl
  }
}

/**
 * The artifact version a tool result names, or null for errors and anything
 * else. Accepts the bare JSON text, the MCP handler's `{result: "<json>"}`
 * envelope, and its `structuredContent` copy.
 */
export function parseHostedArtifactResult(result: unknown): HostedArtifactResult | null {
  const outer = parseJsonObject(result)

  if (!outer || 'error' in outer) {
    return null
  }

  for (const candidate of [outer.structuredContent, outer.result, outer]) {
    const payload = parseJsonObject(candidate)

    if (payload && !('error' in payload)) {
      const parsed = fromPayload(payload)

      if (parsed) {
        return parsed
      }
    }
  }

  return null
}

/**
 * What a URL is to a hosted artifact, read from its shape alone: its viewer page (`…/a/<id>`, any host;
 * callers only trust the id when it matches an artifact they know), or a content ticket
 * (`https://<id>.<frame domain>/__av?t=…`, a five-minute link the content host answers once). Null for
 * anything else.
 */
export function hostedArtifactLink(value: string): { id: string; kind: 'ticket' | 'viewer' } | null {
  let url: URL

  try {
    url = new URL(value)
  } catch {
    return null
  }

  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    return null
  }

  const label = url.hostname.split('.')[0] ?? ''

  if (url.pathname === '/__av' && isHostedArtifactId(label)) {
    return { id: label, kind: 'ticket' }
  }

  const viewer = /^\/a\/([^/]+)\/?$/.exec(url.pathname)

  return viewer && isHostedArtifactId(viewer[1]) ? { id: viewer[1], kind: 'viewer' } : null
}

/** This build's framing policy: development builds also frame the local
 *  artifacts server. */
export function hostedArtifactFramePolicy(): HostedArtifactFramePolicy {
  return { dev: Boolean(import.meta.env.DEV) }
}

/**
 * The origin to frame for this result, or null to show the link card. Null
 * when the content URL is not on this artifact's own origin under an allowed
 * frame domain, or when its ticket has already expired.
 */
export function hostedArtifactFrameFor(
  artifact: HostedArtifactResult,
  {
    completedAt,
    nowSeconds = Date.now() / 1000,
    policy = hostedArtifactFramePolicy()
  }: { completedAt?: number; nowSeconds?: number; policy?: HostedArtifactFramePolicy } = {}
): string | null {
  if (
    typeof completedAt === 'number' &&
    Number.isFinite(completedAt) &&
    completedAt > 0 &&
    nowSeconds - completedAt >= HOSTED_ARTIFACT_TICKET_TTL_SECONDS
  ) {
    return null
  }

  return hostedArtifactFrameOrigin(artifact.contentUrl, policy, artifact.id)
}

/** Longest text a view may submit as one user message. Rejected, never cut. */
export const MAX_UI_MESSAGE_LENGTH = 4000
/** One message per frame per second: a person clicks, a loop floods. */
export const UI_MESSAGE_THROTTLE_MS = 1000

export type UiMessageDecision =
  { kind: 'reject'; reason: 'invalid' | 'throttled' | 'too_long' } | { kind: 'submit'; text: string }

/**
 * Map a view's `ui/message` request to the prompt the composer submits.
 * Only `role: "user"` text blocks count; anything else (images, resources)
 * is not something a typed message can carry, so a message without text is
 * refused rather than half-sent.
 */
export function uiMessageToPrompt(params: unknown, nowMs: number, lastAcceptedMs: number): UiMessageDecision {
  if (!isRecord(params) || params.role !== 'user' || !Array.isArray(params.content)) {
    return { kind: 'reject', reason: 'invalid' }
  }

  const texts = params.content
    .filter(
      (block): block is { text: string; type: 'text' } =>
        isRecord(block) && block.type === 'text' && typeof block.text === 'string'
    )
    .map(block => block.text.trim())
    .filter(Boolean)

  if (texts.length === 0 || texts.length !== params.content.length) {
    return { kind: 'reject', reason: 'invalid' }
  }

  const text = texts.join('\n\n')

  if (text.length > MAX_UI_MESSAGE_LENGTH) {
    return { kind: 'reject', reason: 'too_long' }
  }

  if (nowMs - lastAcceptedMs < UI_MESSAGE_THROTTLE_MS) {
    return { kind: 'reject', reason: 'throttled' }
  }

  return { kind: 'submit', text }
}

// ── Sharing ──────────────────────────────────────────────────────────────────────────────────────
//
// Only the user changes who can see a hosted artifact, on its share page in the Portal (the viewer
// page with `?share=1`). A share the agent asks for is never applied: Hermes answers the call with
// `access_changed: false` and that share page (`share_url`), with any addresses filled in.

const SHARE_TOOL = 'artifact_share'
const UPLOAD_TOOLS = new Set(['artifact_create', 'artifact_update'])
/** The Portal fills in at most this many addresses from a share link. */
const SHARE_PREFILL_MAX = 50
/** Largest page the upload preview renders (the hosted server's per-file cap). */
export const UPLOAD_PREVIEW_MAX_CHARS = 2 * 1024 * 1024

function hostedToolSuffix(toolName: string | undefined): string | null {
  return typeof toolName === 'string' && toolName.startsWith(HOSTED_ARTIFACT_TOOL_PREFIX)
    ? toolName.slice(HOSTED_ARTIFACT_TOOL_PREFIX.length)
    : null
}

export function isHostedArtifactShareTool(toolName: string | undefined): boolean {
  return hostedToolSuffix(toolName) === SHARE_TOOL
}

/** create / update: the user owns what they name, so their card offers Share and shows who can see it. */
export function isHostedArtifactUploadTool(toolName: string | undefined): boolean {
  const tool = hostedToolSuffix(toolName)

  return tool !== null && UPLOAD_TOOLS.has(tool)
}

/** The viewer page with its share sheet open, and `emails` filled in for the user to confirm. */
export function hostedArtifactShareUrl(viewerUrl: string, emails: readonly string[] = []): string {
  const url = new URL(viewerUrl)

  url.searchParams.set('share', '1')

  if (emails.length > 0) {
    url.searchParams.set('invite', emails.join(','))
  }

  url.hash = ''

  return url.toString()
}

/** The addresses typed into the Share panel, split on commas, semicolons or spaces: the valid ones
 *  (lower-cased, de-duplicated, at most the Portal's 50) and anything that is not an address. */
export function parseShareEmails(text: string): { emails: string[]; invalid: string[] } {
  const emails: string[] = []
  const invalid: string[] = []

  for (const part of text.split(/[\s,;]+/).filter(Boolean)) {
    const email = part.toLowerCase()

    if (!PLAIN_EMAIL_RE.test(email) || !email.split('@')[1]?.includes('.')) {
      invalid.push(part)
    } else if (!emails.includes(email)) {
      emails.push(email)
    }
  }

  return { emails: emails.slice(0, SHARE_PREFILL_MAX), invalid }
}

export interface HostedArtifactShareHandoff {
  id: string
  title: string
  viewerUrl: string
  /** The share page, with `emails` filled in for the user to confirm. */
  shareUrl: string
  emails: string[]
  /** The agent asked to let everyone in the organisation view it (the Share panel starts with that on). */
  suggestedOrg: boolean
}

const PLAIN_EMAIL_RE = /^[^\s@,]+@[^\s@,]+$/

/**
 * A share Hermes handed to the user instead of applying it, or null for anything else (an applied
 * narrowing, an error). The share page must be on the viewer page's own origin.
 */
export function parseHostedArtifactShareHandoff(result: unknown): HostedArtifactShareHandoff | null {
  const outer = parseJsonObject(result)

  if (!outer || 'error' in outer) {
    return null
  }

  for (const candidate of [outer.structuredContent, outer.result, outer]) {
    const payload = parseJsonObject(candidate)

    if (!payload || 'error' in payload || payload.access_changed !== false || !isHostedArtifactId(payload.id)) {
      continue
    }

    const viewerUrl = asWebUrl(payload.viewer_url)
    const shareUrl = asWebUrl(payload.share_url)

    if (!viewerUrl || !shareUrl || new URL(viewerUrl).origin !== new URL(shareUrl).origin) {
      continue
    }

    const emails = Array.isArray(payload.invites_to_confirm)
      ? payload.invites_to_confirm
          .filter((e): e is string => typeof e === 'string' && PLAIN_EMAIL_RE.test(e))
          .slice(0, SHARE_PREFILL_MAX)
      : []

    return {
      id: payload.id,
      title: typeof payload.title === 'string' ? payload.title.trim() : '',
      viewerUrl,
      shareUrl,
      emails,
      suggestedOrg: payload.suggested_org === true
    }
  }

  return null
}

/**
 * The page a pending create / update would upload, for a local preview before the user approves it:
 * its `index.html` (or its only HTML file) when sent whole as text. Null for edits, binary files and
 * anything else; other files the page loads (styles, images) are not resolved.
 */
export function uploadPreviewHtml(toolName: string | undefined, args: unknown): string | null {
  if (!isHostedArtifactUploadTool(toolName) || !isRecord(args) || !Array.isArray(args.files)) {
    return null
  }

  const pages = args.files.filter(
    (f): f is { content: string; path: string } =>
      isRecord(f) &&
      typeof f.path === 'string' &&
      /\.html?$/i.test(f.path) &&
      (f.encoding === undefined || f.encoding === 'utf8') &&
      typeof f.content === 'string'
  )

  const page = pages.find(f => /(^|\/)index\.html?$/i.test(f.path)) ?? (pages.length === 1 ? pages[0] : undefined)

  return page && page.content.length > 0 && page.content.length <= UPLOAD_PREVIEW_MAX_CHARS ? page.content : null
}

/** The approval kinds Hermes sends for an upload, which the approval card draws as an upload, not a command. */
export type HostedArtifactUploadKind = 'hosted_artifact_create' | 'hosted_artifact_update'

export function hostedArtifactUploadKind(kind: unknown): HostedArtifactUploadKind | null {
  return kind === 'hosted_artifact_create' || kind === 'hosted_artifact_update' ? kind : null
}
