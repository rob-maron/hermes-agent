import type { Translations } from '@/i18n/types'
import type { HostedArtifactCopy } from '@/i18n/types_hosted_artifact'
import { uploadHeadline } from '@/lib/hosted-artifact-copy'
import { hostedArtifactUploadKind } from '@/lib/hosted-artifacts'
import type { ApprovalRequest } from '@/store/prompts'

type ApprovalCopy = Translations['assistant']['approval']

/** What one approval card says. */
export interface ApprovalLabels {
  icon: string
  heading: string
  headingClassName: string
  approve: string
  reject: string
  alwaysTitle: string
  alwaysDescription: string
  /** The command the "Always allow" confirm repeats; empty when it repeats none. */
  alwaysCommand: string
  /** Details in plain type (an upload's text), not as a command. */
  plainDetails: boolean
  /** The card's text: an upload's sentence in the user's language when its facts came with it. */
  details: string
}

const DETAILS_MARKER = '\n\nDetails:\n'

/** An upload's text with its first sentence in the user's language, keeping the details block (titles
 *  and paths, which need no translation). */
function uploadDetails(details: string, headline: string | null): string {
  if (headline === null) {
    return details
  }

  const at = details.indexOf(DETAILS_MARKER)

  return at === -1 ? headline : `${headline}\n\n${details.slice(at + DETAILS_MARKER.length)}`
}

/**
 * The card's labels. A hosted artifact upload is not a command: it gets its own title, an upload icon,
 * plain text, Upload / Don't upload, and an "Always allow" that says what it lets the agent do. Every
 * other approval keeps the command card.
 */
export function approvalLabels(
  request: Pick<ApprovalRequest, 'command' | 'description' | 'kind' | 'summary'>,
  copy: ApprovalCopy,
  uploadCopy: HostedArtifactCopy,
  showsDescription: boolean,
  details: string
): ApprovalLabels {
  const upload = hostedArtifactUploadKind(request.kind)

  if (upload === null) {
    return {
      alwaysCommand: request.command.trim(),
      alwaysDescription: copy.alwaysDescription(request.description),
      alwaysTitle: copy.alwaysTitle,
      approve: copy.run,
      details,
      heading: showsDescription ? copy.commandDetails : copy.command,
      headingClassName: '',
      icon: 'terminal',
      plainDetails: false,
      reject: copy.reject
    }
  }

  const version = upload === 'hosted_artifact_update'

  return {
    alwaysCommand: '',
    alwaysDescription: version ? uploadCopy.alwaysVersionsDescription : uploadCopy.alwaysUploadsDescription,
    alwaysTitle: version ? uploadCopy.alwaysVersionsTitle : uploadCopy.alwaysUploadsTitle,
    approve: version ? uploadCopy.versionApprove : uploadCopy.uploadApprove,
    details: uploadDetails(details, uploadHeadline(uploadCopy, version, request.summary)),
    heading: version ? uploadCopy.versionApprovalTitle : uploadCopy.uploadApprovalTitle,
    headingClassName: 'font-medium text-(--ui-text-primary)',
    icon: 'cloud-upload',
    plainDetails: true,
    reject: uploadCopy.uploadReject
  }
}

const DETAILS_CLASS =
  'm-0 max-h-40 overflow-auto whitespace-pre-wrap break-words px-2.5 py-2 text-xs leading-relaxed text-(--ui-text-primary)'

/** The card's details: an upload's text in plain type, a command in monospace. */
export function ApprovalDetails({ plain, text }: { plain: boolean; text: string }) {
  if (plain) {
    return (
      <p className={DETAILS_CLASS} data-slot="tool-approval-upload-details">
        {text}
      </p>
    )
  }

  return <pre className={`${DETAILS_CLASS} font-mono`}>{text}</pre>
}
