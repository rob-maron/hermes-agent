import { describe, expect, it } from 'vitest'

import { enHostedArtifact } from '@/i18n/en_hosted_artifact'

import { sharingLabel } from './share-artifact-dialog'

const none = { groups: 0, invites: 0, members: 0, org: false }

describe('sharingLabel', () => {
  it('says only the owner can see a private artifact', () => {
    expect(sharingLabel(enHostedArtifact, none)).toBe('Only you can see it')
  })

  it('counts teammates, groups and people outside; org-wide covers teammates and groups', () => {
    expect(sharingLabel(enHostedArtifact, { ...none, groups: 1, invites: 2, members: 2 })).toBe(
      'Shared with 2 teammates, 1 group and 2 people outside your organisation'
    )
    expect(sharingLabel(enHostedArtifact, { groups: 3, invites: 1, members: 4, org: true })).toBe(
      'Shared with everyone in your organisation and 1 person outside your organisation'
    )
  })
})
