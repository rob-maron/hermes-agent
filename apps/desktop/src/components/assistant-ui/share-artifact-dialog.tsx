import { useStore } from '@nanostores/react'
import { useEffect, useId, useState } from 'react'

import { type HostedArtifactSharingCounts, shareHostedArtifactWithOrg } from '@/api/hosted-artifacts'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useI18n } from '@/i18n'
import type { HostedArtifactCopy } from '@/i18n/types_hosted_artifact'
import { writeClipboardText } from '@/lib/clipboard'
import { audienceList } from '@/lib/hosted-artifact-copy'
import { hostedArtifactShareUrl, parseShareEmails } from '@/lib/hosted-artifacts'
import {
  $hostedArtifactSharing,
  refreshHostedArtifactSharing,
  setHostedArtifactSharing
} from '@/store/hosted-artifact-sharing'

/** "Only you can see it", or "Shared with everyone in your organisation and 1 person outside it". */
export function sharingLabel(
  copy: HostedArtifactCopy,
  sharing: Pick<HostedArtifactSharingCounts, 'groups' | 'invites' | 'members' | 'org'>
): string {
  const list = audienceList(copy, sharing)

  return list ? copy.sharedWith(list) : copy.sharedWithNobody
}

/**
 * Who can see one hosted artifact (null until read, or when it cannot be). Re-read when the window
 * regains focus: the user may be back from the Portal's share page.
 */
export function useHostedArtifactSharing(id: string | null): HostedArtifactSharingCounts | null {
  const entry = useStore($hostedArtifactSharing)[id ?? '']

  useEffect(() => {
    if (!id || typeof window.hermesDesktop?.api !== 'function') {
      return
    }

    void refreshHostedArtifactSharing(id)

    const onFocus = () => void refreshHostedArtifactSharing(id, { force: true })

    window.addEventListener('focus', onFocus)

    return () => window.removeEventListener('focus', onFocus)
  }, [id])

  return id ? (entry?.sharing ?? null) : null
}

export interface ShareArtifactTarget {
  artifactId: string
  title: string
  viewerUrl: string
}

/** What the agent asked for, filled in when the panel opens; nothing is applied until Save. */
export interface ShareSuggestion {
  org?: boolean
  emails?: readonly string[]
}

function openInBrowser(url: string): void {
  if (window.hermesDesktop?.openExternal) {
    void window.hermesDesktop.openExternal(url)
  } else {
    window.open(url, '_blank', 'noopener,noreferrer')
  }
}

interface ShareChoice {
  /** Where it is hosted; null while the page is not saved yet (`saveFirst` saves it). */
  target: ShareArtifactTarget | null
  saveFirst?: () => Promise<ShareArtifactTarget>
  /** Who can see it now, when read (null for a page not saved yet). */
  sharing: HostedArtifactSharingCounts | null
  org: boolean
  emails: string[]
}

/**
 * Apply the panel's choice: save the page first when it is not hosted yet, apply the org switch here,
 * and open the Portal's share page with any typed people filled in. Answers an error, or null.
 */
async function applyShare(copy: HostedArtifactCopy, choice: ShareChoice): Promise<string | null> {
  const { target, saveFirst, sharing, org, emails } = choice
  const where = target ?? (saveFirst ? await saveFirst() : null)

  if (!where) {
    return copy.shareSaveFailed
  }

  // A page saved just now is private: only an org switch turned on changes anything.
  const orgChanged = target ? sharing === null || org !== sharing.org : org

  if (orgChanged) {
    const result = await shareHostedArtifactWithOrg(where.artifactId, org)

    if (!result.ok) {
      return result.error || copy.shareSaveFailed
    }

    if ('viewer_url' in result) {
      setHostedArtifactSharing(where.artifactId, result)
    }
  }

  if (emails.length > 0) {
    openInBrowser(hostedArtifactShareUrl(where.viewerUrl, emails))
  }

  return null
}

/**
 * The Share panel: who can see an artifact, in one place. "Everyone in your organisation can view" is
 * applied here, as the user's own click. People typed into the field are invited on the Portal: Save
 * opens its share page with them filled in, and the user clicks Invite there (inviting someone is the
 * one change that can send the artifact outside the organisation, so it needs the user's browser
 * session, which an agent never holds). A page not saved yet (`target` null) is saved first, on Save.
 */
export function ShareArtifactDialog({
  onOpenChange,
  open,
  saveFirst,
  suggestion,
  target,
  title
}: {
  onOpenChange: (open: boolean) => void
  open: boolean
  saveFirst?: () => Promise<ShareArtifactTarget>
  suggestion?: ShareSuggestion
  target: ShareArtifactTarget | null
  /** The page's title while it is not saved yet (otherwise the target's). */
  title?: string
}) {
  const { t } = useI18n()
  const copy = t.hostedArtifact
  const fieldId = useId()
  const sharing = useHostedArtifactSharing(open && target ? target.artifactId : null)
  const [people, setPeople] = useState('')
  const [org, setOrg] = useState<boolean | null>(null)
  const [saving, setSaving] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Each opening starts from what the agent asked for (if anything) on top of the current sharing.
  useEffect(() => {
    if (open) {
      setPeople((suggestion?.emails ?? []).join(', '))
      setOrg(suggestion?.org === true ? true : null)
      setCopied(false)
      setError(null)
    }
  }, [open, suggestion])

  const orgOn = org ?? sharing?.org ?? false

  // The page's link, private until shared: a page not saved yet is saved first.
  const copyLink = async () => {
    setSaving(true)
    setError(null)

    try {
      const where = target ?? (saveFirst ? await saveFirst() : null)

      if (!where) {
        setError(copy.shareSaveFailed)

        return
      }

      await writeClipboardText(where.viewerUrl)
      setCopied(true)
    } catch {
      setError(copy.shareSaveFailed)
    } finally {
      setSaving(false)
    }
  }

  const typed = parseShareEmails(people)
  const status = target ? (sharing ? sharingLabel(copy, sharing) : copy.shareHint) : copy.shareSavesFirst

  const save = async () => {
    if (typed.invalid.length > 0) {
      setError(copy.shareInvalidEmails(typed.invalid.join(', ')))

      return
    }

    // Nothing chosen for a page not saved yet: nothing to save it for.
    if (!target && !orgOn && typed.emails.length === 0) {
      onOpenChange(false)

      return
    }

    setSaving(true)
    setError(null)

    try {
      const failed = await applyShare(copy, { emails: typed.emails, org: orgOn, saveFirst, sharing, target })

      if (failed) {
        setError(failed)
      } else {
        onOpenChange(false)
      }
    } catch {
      setError(copy.shareSaveFailed)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-md" data-slot="share-artifact-dialog">
        <DialogHeader>
          <DialogTitle>{copy.shareDialogTitle(target?.title || title || copy.untitled)}</DialogTitle>
          <DialogDescription>{status}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-medium" htmlFor={fieldId}>
            {copy.sharePeopleLabel}
          </label>
          <Input
            autoFocus
            id={fieldId}
            onChange={event => {
              setPeople(event.target.value)
              setError(null)
            }}
            placeholder={copy.sharePeoplePlaceholder}
            value={people}
          />
          <span className="text-xs text-muted-foreground">{copy.sharePeopleHint}</span>
        </div>

        <label className="flex items-center justify-between gap-3 text-sm">
          <span>{copy.shareOrgSwitch}</span>
          <Switch checked={orgOn} data-slot="share-artifact-org" onCheckedChange={value => setOrg(value)} />
        </label>

        {suggestion && <span className="text-xs text-muted-foreground">{copy.shareSuggested}</span>}
        {error && (
          <span className="text-xs text-destructive" role="alert">
            {error}
          </span>
        )}

        <DialogFooter className="items-center sm:justify-between">
          <span className="flex items-center gap-1">
            <Button
              data-slot="share-artifact-copy-link"
              disabled={saving}
              onClick={() => void copyLink()}
              size="sm"
              variant="ghost"
            >
              {copied ? t.common.copied : copy.copyLink}
            </Button>
            {target && (
              <Button onClick={() => openInBrowser(hostedArtifactShareUrl(target.viewerUrl))} size="sm" variant="link">
                {copy.shareMoreOptions}
              </Button>
            )}
          </span>
          <span className="flex gap-1.5">
            <Button onClick={() => onOpenChange(false)} size="sm" variant="ghost">
              {t.common.cancel}
            </Button>
            <Button disabled={saving} onClick={() => void save()} size="sm">
              {saving ? copy.shareSaving : typed.emails.length > 0 ? copy.shareInviteOnPortal : copy.shareSave}
            </Button>
          </span>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** A Share button that opens the Share panel for one artifact. */
export function ShareArtifactButton({
  className,
  suggestion,
  target
}: {
  className?: string
  suggestion?: ShareSuggestion
  target: ShareArtifactTarget
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)

  return (
    <>
      <Button
        className={className}
        data-slot="aui_hosted-artifact-share"
        onClick={() => setOpen(true)}
        size="sm"
        variant="outline"
      >
        {t.hostedArtifact.share}
      </Button>
      <ShareArtifactDialog onOpenChange={setOpen} open={open} suggestion={suggestion} target={target} />
    </>
  )
}
