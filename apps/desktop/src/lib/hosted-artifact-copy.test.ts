import { describe, expect, it } from 'vitest'

import { enHostedArtifact } from '@/i18n/en_hosted_artifact'

import { formatBytes, uploadHeadline } from './hosted-artifact-copy'

const audience = { groups: 0, invites: 1, members: 2, org: false, pin: 3 }

describe('uploadHeadline', () => {
  it('says a new artifact in the user’s language from its facts', () => {
    expect(uploadHeadline(enHostedArtifact, false, { bytes: 12288, files: 1 })).toBe(
      "Hermes made a page you can open from anywhere and share when you're ready. It's saved privately to your Nous account, so only you can see it until you share it. (1 file, 12 KB)"
    )
  })

  it('says who will see a new version', () => {
    expect(uploadHeadline(enHostedArtifact, true, { audience, edits: 2 })).toBe(
      "This replaces what's on the page (2 edits). It's shared with 2 teammates and 1 person outside your organisation, who stay on version 3 until you change the version shown."
    )
    expect(uploadHeadline(enHostedArtifact, true, { files: 3 })).toBe(
      "This replaces what's on the page (3 files). Everyone it's shared with may see it."
    )
    expect(
      uploadHeadline(enHostedArtifact, true, { audience: { ...audience, invites: 0, members: 0 }, edits: 1 })
    ).toContain('(1 edit). Only you can see it.')
  })

  it('falls back to Hermes’s own text when the facts are missing or malformed', () => {
    expect(uploadHeadline(enHostedArtifact, false, undefined)).toBeNull()
    expect(uploadHeadline(enHostedArtifact, false, { files: -1 })).toBeNull()
    expect(uploadHeadline(enHostedArtifact, true, { audience: { org: 'yes' }, edits: 1 })).toBeNull()
  })

  it('formats sizes', () => {
    expect([formatBytes(12), formatBytes(2048), formatBytes(3 * 1024 * 1024)]).toEqual(['12 B', '2 KB', '3.0 MB'])
  })
})
