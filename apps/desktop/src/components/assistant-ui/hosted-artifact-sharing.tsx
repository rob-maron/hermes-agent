import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { type FC, useMemo, useState } from 'react'

import { WIDGET_SHELL_CLASS } from '@/components/chat/widget-shell'
import { Button } from '@/components/ui/button'
import { PreviewDocumentFrame } from '@/components/ui/preview-document-frame'
import { useI18n } from '@/i18n'
import { type HostedArtifactShareHandoff, parseHostedArtifactShareHandoff } from '@/lib/hosted-artifacts'
import { cn } from '@/lib/utils'

import { ShareArtifactButton, sharingLabel, useHostedArtifactSharing } from './share-artifact-dialog'

/** Same column cap as the artifact card. */
const MAX_CARD_WIDTH = 960
const PREVIEW_HEIGHT = 420

/**
 * A share the agent asked for, which Hermes did not apply: nothing has changed, and Share opens the
 * Share panel with what the agent asked for filled in.
 */
export const HostedArtifactShareCard: FC<{ handoff: HostedArtifactShareHandoff }> = ({ handoff }) => {
  const { t } = useI18n()
  const copy = t.hostedArtifact
  const sharing = useHostedArtifactSharing(handoff.id)
  const title = handoff.title || copy.untitled
  const suggestion = useMemo(() => ({ emails: handoff.emails, org: handoff.suggestedOrg }), [handoff])

  return (
    <div className="my-1.5 w-full" data-slot="aui_hosted-artifact-share-card" style={{ maxWidth: MAX_CARD_WIDTH }}>
      <div className={cn(WIDGET_SHELL_CLASS, 'flex w-full items-center gap-2.5 overflow-hidden')}>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[length:var(--conversation-text-font-size)] font-medium text-foreground">
            {copy.shareCardTitle(title)}
          </span>
          <span className="block break-words text-[length:var(--conversation-tool-font-size)] text-muted-foreground">
            {handoff.emails.length > 0 ? copy.shareCardInvites(handoff.emails.join(', ')) : copy.shareCardBody}
          </span>
          {sharing && (
            <span className="block truncate text-[length:var(--conversation-tool-font-size)] text-muted-foreground">
              {sharingLabel(copy, sharing)}
            </span>
          )}
        </span>
        <ShareArtifactButton
          className="shrink-0"
          suggestion={suggestion}
          target={{ artifactId: handoff.id, title, viewerUrl: handoff.viewerUrl }}
        />
      </div>
    </div>
  )
}

type SharePartProps = ToolCallMessagePartProps & { completedAt?: number; timestamp?: number }

/** Tool-part entry for `artifact_share`: the Share card for a handed-off share, else `fallback`. */
export function HostedArtifactShareToolPart({ fallback, ...props }: SharePartProps & { fallback: FC<SharePartProps> }) {
  const { isError, result } = props

  const handoff = useMemo(
    () => (isError || result === undefined ? null : parseHostedArtifactShareHandoff(result)),
    [isError, result]
  )

  if (!handoff) {
    const Fallback = fallback

    return <Fallback {...props} />
  }

  return <HostedArtifactShareCard handoff={handoff} />
}

/**
 * The page a pending upload would send, rendered locally so the user can look before approving it.
 * Same sandbox as the artifact panel: scripts run in an opaque origin with no access to the app.
 */
export function HostedArtifactUploadPreview({ html }: { html: string }) {
  const { t } = useI18n()
  const copy = t.hostedArtifact
  const [open, setOpen] = useState(false)

  return (
    <div className="my-1 w-full" data-slot="aui_hosted-artifact-upload-preview" style={{ maxWidth: MAX_CARD_WIDTH }}>
      <Button
        aria-expanded={open}
        data-slot="aui_hosted-artifact-upload-preview-toggle"
        onClick={() => setOpen(value => !value)}
        size="sm"
        variant="ghost"
      >
        {open ? copy.hideUploadPreview : copy.previewUpload}
      </Button>
      {open && (
        <PreviewDocumentFrame
          className="mt-1.5 block w-full rounded-md border border-border/60 bg-white"
          sandbox="allow-scripts"
          srcDoc={html}
          // Foreign generated HTML assumes a light canvas, as in the artifact panel.
          style={{ colorScheme: 'light', height: PREVIEW_HEIGHT }}
          title={copy.uploadPreviewTitle}
        />
      )}
    </div>
  )
}
