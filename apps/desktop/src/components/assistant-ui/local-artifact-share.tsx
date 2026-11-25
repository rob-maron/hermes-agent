import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import {
  ShareArtifactDialog,
  sharingLabel,
  useHostedArtifactSharing
} from '@/components/assistant-ui/share-artifact-dialog'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { CloudUpload, Users } from '@/lib/icons'
import { hostedArtifactsAvailable, publishArtifactVersion } from '@/lib/local-artifact-publish'
import type { ArtifactRecord, ArtifactVersion } from '@/store/artifacts'
import { notifyError } from '@/store/notifications'
import {
  $hostedByContent,
  $publishedArtifacts,
  type PublishedArtifact,
  publishedFor
} from '@/store/published-artifacts'

/**
 * Sharing a page made in a chat (a local artifact). The page stays on this computer until the user acts:
 * Share opens the Share panel, whose Save or Copy link saves it privately to their Nous account first;
 * Save update uploads a changed page as a new version of the one already saved. Shown only on a desktop
 * whose account may save pages (the backend checks again).
 */

/** Whether this desktop's account may save pages; false until the (cached) answer arrives. */
export function useHostedArtifactsAvailable(): boolean {
  const [available, setAvailable] = useState(false)

  useEffect(() => {
    let live = true

    if (typeof window.hermesDesktop?.api === 'function') {
      void hostedArtifactsAvailable().then(ok => {
        if (live) {
          setAvailable(ok)
        }
      })
    }

    return () => {
      live = false
    }
  }, [])

  return available
}

/** What a local artifact is saved as: its own save, else a copy the agent already uploaded. */
export function usePublishedArtifact(record: ArtifactRecord): PublishedArtifact | undefined {
  const own = useStore($publishedArtifacts)
  const byContent = useStore($hostedByContent)

  return publishedFor(
    record.id,
    record.versions.map(v => v.hash),
    own,
    byContent
  )
}

/** "Only you can see it" for a page not saved yet; once saved, who can see it (null until read). */
export function useLocalArtifactAudience(published: PublishedArtifact | undefined): null | string {
  const { t } = useI18n()
  const copy = t.hostedArtifact
  const sharing = useHostedArtifactSharing(published ? published.hostedId : null)

  if (!published) {
    return copy.sharedWithNobody
  }

  return sharing ? sharingLabel(copy, sharing) : null
}

export function LocalArtifactShare({
  buttonClassName,
  record,
  version
}: {
  buttonClassName: string
  record: ArtifactRecord
  /** The version this control shares (the one on screen). */
  version: ArtifactVersion
}) {
  const { t } = useI18n()
  const copy = t.hostedArtifact
  const available = useHostedArtifactsAvailable()
  const published = usePublishedArtifact(record)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)

  if (!available) {
    return null
  }

  const changed = published !== undefined && published.hash !== version.hash

  const saveUpdate = async () => {
    setBusy(true)

    try {
      await publishArtifactVersion(record, version, published)
    } catch (error) {
      notifyError(error, copy.publishFailed)
    } finally {
      setBusy(false)
    }
  }

  // Not saved yet: the Share panel's Save or Copy link saves this version first.
  const saveFirst = async () => {
    const saved = await publishArtifactVersion(record, version, published)

    return { artifactId: saved.hostedId, title: record.title, viewerUrl: saved.viewerUrl }
  }

  return (
    <>
      {changed && (
        <Tip label={copy.audienceUnknown}>
          <button
            aria-label={copy.publishUpdate}
            className={buttonClassName}
            data-slot="artifact-save-update"
            disabled={busy}
            onClick={() => void saveUpdate()}
            type="button"
          >
            <CloudUpload className="size-3" />
            <span>{busy ? copy.publishing : copy.publishUpdate}</span>
          </button>
        </Tip>
      )}
      <Tip label={published ? copy.shareHint : copy.publishHint}>
        <button
          aria-label={copy.share}
          className={buttonClassName}
          data-slot="artifact-share"
          disabled={busy}
          onClick={() => setOpen(true)}
          type="button"
        >
          <Users className="size-3" />
          <span>{copy.share}</span>
        </button>
      </Tip>
      <ShareArtifactDialog
        onOpenChange={setOpen}
        open={open}
        saveFirst={published ? undefined : saveFirst}
        target={
          published ? { artifactId: published.hostedId, title: record.title, viewerUrl: published.viewerUrl } : null
        }
        title={record.title}
      />
    </>
  )
}
