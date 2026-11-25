import { describe, expect, it } from 'vitest'

import {
  hostedArtifactShareUrl,
  isHostedArtifactCardTool,
  isHostedArtifactShareTool,
  isHostedArtifactUploadTool,
  parseHostedArtifactShareHandoff,
  UPLOAD_PREVIEW_MAX_CHARS,
  uploadPreviewHtml
} from './hosted-artifacts'

const ID = '01j9z8y7x6w5v4t3s2r1q0p9n8'
const VIEWER_URL = `https://portal.example/a/${ID}`
const SHARE_URL = `${VIEWER_URL}?share=1&invite=dana%40example.com`
const CREATE = 'mcp__hermes_artifacts__artifact_create'

const HANDOFF = {
  access_changed: false,
  id: ID,
  invites_to_confirm: ['dana@example.com'],
  message: 'Who can see this artifact was NOT changed',
  share_url: SHARE_URL,
  title: 'Chart',
  viewer_url: VIEWER_URL
}

describe('hosted artifact share and upload tools', () => {
  it('names the share tool and the two tools whose artifact the user owns', () => {
    expect(isHostedArtifactShareTool('mcp__hermes_artifacts__artifact_share')).toBe(true)
    expect(isHostedArtifactShareTool('mcp__other__artifact_share')).toBe(false)
    expect(isHostedArtifactUploadTool(CREATE)).toBe(true)
    expect(isHostedArtifactUploadTool('mcp__hermes_artifacts__artifact_show')).toBe(false)
    // Unchanged: share is not an artifact card.
    expect(isHostedArtifactCardTool('mcp__hermes_artifacts__artifact_share')).toBe(false)
  })

  it('opens the share sheet on the viewer page', () => {
    expect(hostedArtifactShareUrl(`${VIEWER_URL}#top`)).toBe(`${VIEWER_URL}?share=1`)
  })
})

describe('parseHostedArtifactShareHandoff', () => {
  it('reads a share Hermes handed to the user, from the handler envelope or the bare body', () => {
    const expected = {
      emails: ['dana@example.com'],
      id: ID,
      shareUrl: SHARE_URL,
      suggestedOrg: false,
      title: 'Chart',
      viewerUrl: VIEWER_URL
    }

    expect(parseHostedArtifactShareHandoff(JSON.stringify({ result: JSON.stringify(HANDOFF) }))).toEqual(expected)
    expect(parseHostedArtifactShareHandoff(HANDOFF)).toEqual(expected)
  })

  it('refuses an applied share, an error, another origin and a bad id', () => {
    expect(parseHostedArtifactShareHandoff({ ...HANDOFF, access_changed: true })).toBeNull()
    expect(parseHostedArtifactShareHandoff(JSON.stringify({ error: 'nope' }))).toBeNull()
    expect(parseHostedArtifactShareHandoff({ ...HANDOFF, share_url: 'https://evil.example/a?share=1' })).toBeNull()
    expect(parseHostedArtifactShareHandoff({ ...HANDOFF, id: '../x' })).toBeNull()
    expect(parseHostedArtifactShareHandoff(JSON.stringify({ result: 'ok' }))).toBeNull()
  })

  it('keeps only plain addresses', () => {
    const parsed = parseHostedArtifactShareHandoff({
      ...HANDOFF,
      invites_to_confirm: ['a@example.com', 'a b@example.com', 'x,y@z', 7]
    })

    expect(parsed?.emails).toEqual(['a@example.com'])
  })
})

describe('uploadPreviewHtml', () => {
  const page = (path: string, content = '<h1>Hi</h1>', encoding?: string) => ({
    content,
    path,
    ...(encoding ? { encoding } : {})
  })

  it('previews the index page of a whole upload', () => {
    const args = { files: [page('style.css', 'body{}'), page('index.html', '<h1>Index</h1>', 'utf8'), page('b.html')] }

    expect(uploadPreviewHtml(CREATE, args)).toBe('<h1>Index</h1>')
    expect(uploadPreviewHtml(CREATE, { files: [page('only.htm')] })).toBe('<h1>Hi</h1>')
  })

  it('shows nothing for edits, binaries, other tools or a page too large', () => {
    expect(uploadPreviewHtml(CREATE, { edits: [{ new: 'x', old: 'y', path: 'index.html' }] })).toBeNull()
    expect(uploadPreviewHtml(CREATE, { files: [page('index.html', 'PGgxPg==', 'base64')] })).toBeNull()
    expect(uploadPreviewHtml(CREATE, { files: [page('a.html'), page('b.html')] })).toBeNull()
    expect(uploadPreviewHtml('mcp__hermes_artifacts__artifact_get', { files: [page('index.html')] })).toBeNull()
    expect(
      uploadPreviewHtml(CREATE, { files: [page('index.html', 'x'.repeat(UPLOAD_PREVIEW_MAX_CHARS + 1))] })
    ).toBeNull()
  })
})

describe('hostedArtifactUploadKind', () => {
  it('knows the two upload kinds and nothing else', async () => {
    const { hostedArtifactUploadKind } = await import('./hosted-artifacts')

    expect(hostedArtifactUploadKind('hosted_artifact_create')).toBe('hosted_artifact_create')
    expect(hostedArtifactUploadKind('hosted_artifact_update')).toBe('hosted_artifact_update')
    expect(hostedArtifactUploadKind('mcp_elicitation')).toBeNull()
    expect(hostedArtifactUploadKind(undefined)).toBeNull()
  })
})

describe('the Share panel’s addresses', () => {
  it('splits, lower-cases and de-duplicates, and reports what is not an address', async () => {
    const { parseShareEmails, hostedArtifactShareUrl } = await import('./hosted-artifacts')

    expect(parseShareEmails('Dana@Example.com, eli@example.org; dana@example.com  nope')).toEqual({
      emails: ['dana@example.com', 'eli@example.org'],
      invalid: ['nope']
    })
    expect(parseShareEmails('a@b').invalid).toEqual(['a@b'])
    expect(hostedArtifactShareUrl(VIEWER_URL, ['a@example.com'])).toBe(`${VIEWER_URL}?share=1&invite=a%40example.com`)
  })

  it('a handed-off share says whether the agent asked for the whole org', () => {
    expect(parseHostedArtifactShareHandoff({ ...HANDOFF, suggested_org: true })?.suggestedOrg).toBe(true)
    expect(parseHostedArtifactShareHandoff(HANDOFF)?.suggestedOrg).toBe(false)
  })
})
