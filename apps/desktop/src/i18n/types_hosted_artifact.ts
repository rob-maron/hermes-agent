/** Copy for a server-hosted artifact a tool result names. */
export interface HostedArtifactCopy {
  /** Fallback title when the result carries none. */
  untitled: string
  version: (version: number) => string
  /** Badge when a later result already named a newer version. */
  newerVersion: (version: number) => string
  openViewer: string
  /** Shown instead of the live frame (expired ticket, or a build that does not frame). */
  linkOnly: string
  frameTitle: (title: string) => string
  /** Above a message an artifact asks to send; the user must confirm it. */
  messagePrompt: string
  messageSend: string
  messageDismiss: string
  /** The artifact panel's Publish button (desktop): upload a local artifact to hosted artifacts. */
  publish: string
  /** Its label once the artifact changed since its last publish (a new version). */
  publishUpdate: string
  publishing: string
  /** Its label when the shown version is the one published: opens the hosted page. */
  openPublished: string
  /** Tooltip before publishing: what Publish does. */
  publishHint: string
  publishFailed: string
  /** Opens the artifact's share page in the Portal: the only place sharing changes. */
  share: string
  shareHint: string
  /** Who can see an artifact the user owns. */
  sharedWithNobody: string
  sharedWith: (audience: string) => string
  audienceOrg: string
  audienceTeammates: (count: number) => string
  audienceGroups: (count: number) => string
  audienceOutside: (count: number) => string
  /** The audience parts joined as a list ("a, b and c"). */
  joinList: (parts: string[]) => string
  /** A share the agent asked for, which Hermes did not apply: the user finishes it on the share page. */
  shareCardTitle: (title: string) => string
  shareCardBody: string
  shareCardInvites: (emails: string) => string
  /** A pending upload's local preview, before the user approves it. */
  previewUpload: string
  hideUploadPreview: string
  uploadPreviewTitle: string
  /** The approval card for an upload the agent asks for (drawn as an upload, not a command). */
  uploadApprovalTitle: string
  versionApprovalTitle: string
  uploadApprove: string
  /** The approve button on a new version's card. */
  versionApprove: string
  uploadReject: string
  /** Its "Always allow" confirm: what it lets the agent do without asking. */
  alwaysUploadsTitle: string
  alwaysUploadsDescription: string
  alwaysVersionsTitle: string
  alwaysVersionsDescription: string
  /** The Share panel (who can see an artifact; people are confirmed on the Portal). */
  shareDialogTitle: (title: string) => string
  sharePeopleLabel: string
  sharePeoplePlaceholder: string
  sharePeopleHint: string
  shareOrgSwitch: string
  /** Shown when the panel opened with what the agent asked for. */
  shareSuggested: string
  shareMoreOptions: string
  /** The Share panel's button that copies the page's link (saving a page not saved yet first). */
  copyLink: string
  shareSave: string
  shareSaving: string
  /** The save button when people were typed: it opens the Portal to invite them. */
  shareInviteOnPortal: string
  shareInvalidEmails: (list: string) => string
  shareSaveFailed: string
  /** The Share panel for a page not saved yet: Save saves it first. */
  shareSavesFirst: string
  /** An upload card's sentence, built from the facts Hermes sends with it. */
  uploadCreateText: (facts: string) => string
  uploadUpdateText: (facts: string, audience: string) => string
  uploadFiles: (count: number, size: string) => string
  uploadFileCount: (count: number) => string
  uploadEdits: (count: number) => string
  audienceOnlyYou: string
  audienceLatest: (audience: string) => string
  audiencePinned: (audience: string, version: number) => string
  audienceUnknown: string
}
