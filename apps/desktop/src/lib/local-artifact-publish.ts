import { getHostedArtifactsStatus, publishHostedArtifact } from '@/api/hosted-artifacts'
import type { ArtifactRecord, ArtifactVersion } from '@/store/artifacts'
import { type PublishedArtifact, recordPublishedArtifact } from '@/store/published-artifacts'

/**
 * Saving a page made in a chat (a local artifact) to the user's Nous hosted artifacts. Nothing here runs
 * on its own: the user's click on Share (then Save or Copy link) or Save update is the consent.
 */

const HOSTED_TITLE_MAX_CHARS = 200

/** Wrap an HTML fragment in a minimal document shell; full documents pass
 *  through untouched. Keeps generated fragments (no <html>/<body>) rendering
 *  with sane defaults instead of quirks-mode soup. */
export function composeArtifactHtml(content: string): string {
  if (/<html[\s>]|<!doctype\s+html/i.test(content)) {
    return content
  }

  return [
    '<!doctype html>',
    '<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
    '<style>body{margin:0;font-family:system-ui,sans-serif}</style></head><body>',
    content,
    '</body></html>'
  ].join('\n')
}

// The hosted artifacts answer is per account and changes rarely (sign-in, plan): ask once per few
// minutes, not per card. A failed read hides Share; the backend refuses a publish anyway.
const HOSTED_STATUS_TTL_MS = 5 * 60_000
let hostedStatus: { at: number; available: Promise<boolean> } | null = null

/** Whether this desktop's account may save pages (signed in to the Nous Portal with artifacts access). */
export function hostedArtifactsAvailable(): Promise<boolean> {
  if (!hostedStatus || Date.now() - hostedStatus.at > HOSTED_STATUS_TTL_MS) {
    hostedStatus = {
      at: Date.now(),
      available: getHostedArtifactsStatus()
        .then(status => status.available === true)
        .catch(() => false)
    }
  }

  return hostedStatus.available
}

/** Test seam: forget the cached hosted artifacts answer. */
export function resetHostedArtifactsStatus(): void {
  hostedStatus = null
}

/** Upload this version: a new hosted artifact, or a new version of the one it is published as. */
export async function publishArtifactVersion(
  record: ArtifactRecord,
  version: ArtifactVersion,
  published: PublishedArtifact | undefined
): Promise<PublishedArtifact> {
  const result = await publishHostedArtifact({
    title: record.title.slice(0, HOSTED_TITLE_MAX_CHARS),
    html: composeArtifactHtml(version.content),
    ...(published ? { artifact_id: published.hostedId, base_version: published.version } : {})
  })

  if (!result.ok) {
    throw new Error(result.error)
  }

  const saved = {
    hash: version.hash,
    hostedId: result.id,
    publishedAt: Date.now(),
    version: result.version,
    viewerUrl: result.viewer_url
  }

  recordPublishedArtifact(record.id, saved)

  return saved
}
