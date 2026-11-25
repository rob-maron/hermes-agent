import type { HostedArtifactCopy } from '@/i18n/types_hosted_artifact'

/** Who can see an artifact, as counts (the backend's sharing answer, or an upload card's facts). */
export interface AudienceCounts {
  groups: number
  invites: number
  members: number
  org: boolean
  pin?: 'latest' | number
}

/** "everyone in your organisation and 1 person outside it"; empty when only the owner can see it.
 *  Org-wide already covers named teammates and groups, so they are not counted again. */
export function audienceList(copy: HostedArtifactCopy, counts: AudienceCounts): string {
  const parts: string[] = []

  if (counts.org) {
    parts.push(copy.audienceOrg)
  } else {
    if (counts.members > 0) {
      parts.push(copy.audienceTeammates(counts.members))
    }

    if (counts.groups > 0) {
      parts.push(copy.audienceGroups(counts.groups))
    }
  }

  if (counts.invites > 0) {
    parts.push(copy.audienceOutside(counts.invites))
  }

  return parts.length > 0 ? copy.joinList(parts) : ''
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`
  }

  return bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null

function audienceFrom(value: unknown): AudienceCounts | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const v = value as Record<string, unknown>
  const [members, groups, invites] = [count(v.members), count(v.groups), count(v.invites)]
  const pin = v.pin === 'latest' || (count(v.pin) ?? 0) >= 1 ? (v.pin as 'latest' | number) : null

  if (typeof v.org !== 'boolean' || members === null || groups === null || invites === null || pin === null) {
    return null
  }

  return { groups, invites, members, org: v.org, pin }
}

function newVersionLine(copy: HostedArtifactCopy, audience: AudienceCounts | null): string {
  if (!audience) {
    return copy.audienceUnknown
  }

  const list = audienceList(copy, audience)

  if (!list) {
    return copy.audienceOnlyYou
  }

  return audience.pin === 'latest' ? copy.audienceLatest(list) : copy.audiencePinned(list, Number(audience.pin))
}

/**
 * An upload card's sentence in the user's language, from the facts Hermes sends with it
 * (`{files, bytes}` or `{edits}`, plus `audience` for a new version). Null when the facts are missing
 * or malformed: the card then shows Hermes's own text.
 */
export function uploadHeadline(copy: HostedArtifactCopy, version: boolean, summary: unknown): string | null {
  if (!summary || typeof summary !== 'object') {
    return null
  }

  const s = summary as Record<string, unknown>
  const [files, bytes, edits] = [count(s.files), count(s.bytes), count(s.edits)]
  let facts: string

  if (edits !== null) {
    facts = copy.uploadEdits(edits)
  } else if (files !== null) {
    facts = bytes === null ? copy.uploadFileCount(files) : copy.uploadFiles(files, formatBytes(bytes))
  } else {
    return null
  }

  if (!version) {
    return copy.uploadCreateText(facts)
  }

  const audience = s.audience === undefined ? null : audienceFrom(s.audience)

  if (s.audience !== undefined && audience === null) {
    return null
  }

  return copy.uploadUpdateText(facts, newVersionLine(copy, audience))
}
