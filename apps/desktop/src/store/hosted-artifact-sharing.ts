import { map } from 'nanostores'

import { getHostedArtifactSharing, type HostedArtifactSharingCounts } from '@/api/hosted-artifacts'

/**
 * Who can see each hosted artifact the transcript shows, read from the backend. Sharing changes only
 * on the Portal's share page, so a card re-reads when the window regains focus (the user is back from
 * that page); cards naming the same artifact share one read.
 */
interface SharingEntry {
  at: number
  /** Null when it could not be read (or hosted artifacts are not available here). */
  sharing: HostedArtifactSharingCounts | null
}

export const $hostedArtifactSharing = map<Record<string, SharingEntry>>({})

const FRESH_MS = 30_000
const inflight = new Map<string, Promise<void>>()

export function refreshHostedArtifactSharing(id: string, { force = false }: { force?: boolean } = {}): Promise<void> {
  const cached = $hostedArtifactSharing.get()[id]

  if (!force && cached && Date.now() - cached.at < FRESH_MS) {
    return Promise.resolve()
  }

  const running = inflight.get(id)

  if (running) {
    return running
  }

  const run = getHostedArtifactSharing(id)
    .then(result => (result.ok ? result : null))
    .catch(() => null)
    .then(sharing => {
      $hostedArtifactSharing.setKey(id, { at: Date.now(), sharing })
    })
    .finally(() => inflight.delete(id))

  inflight.set(id, run)

  return run
}

/** Who can see it, as the backend just answered after a change. */
export function setHostedArtifactSharing(id: string, sharing: HostedArtifactSharingCounts | null): void {
  $hostedArtifactSharing.setKey(id, { at: Date.now(), sharing })
}

/** Test seam. */
export function resetHostedArtifactSharing(): void {
  inflight.clear()
  $hostedArtifactSharing.set({})
}
