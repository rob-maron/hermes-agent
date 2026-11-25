import { type Codec, persistentAtom } from '@/lib/persisted'

/** What a local artifact was last published as: the hosted artifact, its version, and the local
 *  version (content hash) that was uploaded, so the panel can tell "published" from "changed since". */
export interface PublishedArtifact {
  hostedId: string
  version: number
  viewerUrl: string
  hash: string
  publishedAt: number
}

const STORAGE_KEY = 'hermes.desktop.publishedArtifacts'
const MAX_ENTRIES = 200
const HOSTED_ID_RE = /^[0-9a-hjkmnp-tv-z]{26}$/

function isPublished(value: unknown): value is PublishedArtifact {
  if (!value || typeof value !== 'object') {
    return false
  }

  const v = value as Record<string, unknown>

  return (
    typeof v.hostedId === 'string' &&
    HOSTED_ID_RE.test(v.hostedId) &&
    Number.isInteger(v.version) &&
    (v.version as number) >= 1 &&
    typeof v.viewerUrl === 'string' &&
    /^https?:\/\//.test(v.viewerUrl) &&
    typeof v.hash === 'string' &&
    typeof v.publishedAt === 'number'
  )
}

const codec: Codec<Record<string, PublishedArtifact>> = {
  decode: raw => {
    try {
      const parsed: unknown = JSON.parse(raw)

      if (!parsed || typeof parsed !== 'object') {
        return {}
      }

      return Object.fromEntries(Object.entries(parsed).filter(([, value]) => isPublished(value)))
    } catch {
      return {}
    }
  },
  encode: value => (Object.keys(value).length ? JSON.stringify(value) : null)
}

/** Local artifact id → what it was last published as. Persisted so a later version still publishes as
 *  a new version of the same hosted artifact after the app restarts. */
export const $publishedArtifacts = persistentAtom<Record<string, PublishedArtifact>>(STORAGE_KEY, {}, codec)

export function recordPublishedArtifact(localId: string, published: PublishedArtifact): void {
  const next = { ...$publishedArtifacts.get(), [localId]: published }
  const ids = Object.keys(next)

  // Keep the newest entries: an old local artifact that drops out publishes as a new one.
  if (ids.length > MAX_ENTRIES) {
    ids
      .sort((a, b) => next[a]!.publishedAt - next[b]!.publishedAt)
      .slice(0, ids.length - MAX_ENTRIES)
      .forEach(id => delete next[id])
  }

  $publishedArtifacts.set(next)
}

const CONTENT_STORAGE_KEY = 'hermes.desktop.hostedArtifactContent'

/** Content hash of a page the agent uploaded → the hosted artifact and version it went up as. Lets the
 *  artifact panel's Publish reuse what the agent already uploaded instead of making a second copy. */
export const $hostedByContent = persistentAtom<Record<string, PublishedArtifact>>(CONTENT_STORAGE_KEY, {}, codec)

export function recordHostedContent(published: PublishedArtifact): void {
  const next = { ...$hostedByContent.get(), [published.hash]: published }
  const hashes = Object.keys(next)

  if (hashes.length > MAX_ENTRIES) {
    hashes
      .sort((a, b) => next[a]!.publishedAt - next[b]!.publishedAt)
      .slice(0, hashes.length - MAX_ENTRIES)
      .forEach(hash => delete next[hash])
  }

  $hostedByContent.set(next)
}

/**
 * What a local artifact is published as: its own Publish record, else the newest of its versions the
 * agent already uploaded (so Publish offers "Open published" or "Publish update" for that hosted
 * artifact rather than uploading a second copy).
 */
export function publishedFor(
  localId: string,
  versionHashes: readonly string[],
  own: Record<string, PublishedArtifact> = $publishedArtifacts.get(),
  byContent: Record<string, PublishedArtifact> = $hostedByContent.get()
): PublishedArtifact | undefined {
  if (own[localId]) {
    return own[localId]
  }

  for (const hash of [...versionHashes].reverse()) {
    if (byContent[hash]) {
      return byContent[hash]
    }
  }

  return undefined
}
