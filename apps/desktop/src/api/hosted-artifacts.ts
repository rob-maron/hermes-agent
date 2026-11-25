import { capabilityScoped, type ProfileScope } from './client'

/** Whether this profile's account may publish hosted artifacts (signed in to the Nous Portal, with
 *  artifacts access). `reason` explains a `false` (`not_logged_in`, `not_enabled`, ...). */
export interface HostedArtifactsStatus {
  available: boolean
  reason: string
}

export interface HostedArtifactPublishRequest {
  title: string
  /** The full HTML document. */
  html: string
  /** An earlier publish of this artifact: upload a new version of it. */
  artifact_id?: string
  base_version?: number
}

export type HostedArtifactPublishResult =
  { ok: true; id: string; version: number; viewer_url: string } | { ok: false; error: string; reason?: string }

export function getHostedArtifactsStatus(profile?: ProfileScope): Promise<HostedArtifactsStatus> {
  return window.hermesDesktop.api<HostedArtifactsStatus>({
    ...capabilityScoped(profile),
    path: '/api/hosted-artifacts/status'
  })
}

/** Upload one artifact the user chose to publish. Their click is the consent: the backend runs it as
 *  their own call, and refuses unless the account may publish hosted artifacts. */
export function publishHostedArtifact(
  request: HostedArtifactPublishRequest,
  profile?: ProfileScope
): Promise<HostedArtifactPublishResult> {
  return window.hermesDesktop.api<HostedArtifactPublishResult>({
    ...capabilityScoped(profile),
    path: '/api/hosted-artifacts/publish',
    method: 'POST',
    body: request,
    // First use connects the hosted server, then uploads up to 2 MB.
    timeoutMs: 90_000
  })
}

/** Who can see one hosted artifact (counts only), and its share page. */
export interface HostedArtifactSharingCounts {
  ok: true
  viewer_url: string
  share_url: string
  org: boolean
  members: number
  groups: number
  invites: number
  pin: 'latest' | number
}

export type HostedArtifactSharing = HostedArtifactSharingCounts | { ok: false; reason?: string }

/** Read who can see one hosted artifact (a read-only call on the backend; no approval card). */
export function getHostedArtifactSharing(artifactId: string, profile?: ProfileScope): Promise<HostedArtifactSharing> {
  return window.hermesDesktop.api<HostedArtifactSharing>({
    ...capabilityScoped(profile),
    path: `/api/hosted-artifacts/sharing?artifact_id=${encodeURIComponent(artifactId)}`,
    // First use connects the hosted server.
    timeoutMs: 45_000
  })
}

export type HostedArtifactOrgShareResult = HostedArtifactSharingCounts | { ok: true } | { ok: false; error: string }

/** The Share panel's switch: let everyone in the artifact's organisation view it, or stop that. The
 *  click is the consent; specific people are never invited from here (the Portal confirms them). */
export function shareHostedArtifactWithOrg(
  artifactId: string,
  org: boolean,
  profile?: ProfileScope
): Promise<HostedArtifactOrgShareResult> {
  return window.hermesDesktop.api<HostedArtifactOrgShareResult>({
    ...capabilityScoped(profile),
    path: '/api/hosted-artifacts/share',
    method: 'POST',
    body: { artifact_id: artifactId, org },
    timeoutMs: 45_000
  })
}
