import { afterEach, describe, expect, it } from 'vitest'

import { $publishedArtifacts, recordPublishedArtifact } from './published-artifacts'

const ID = '01jabcdefghjkmnpqrstvwxyz0'
const entry = (publishedAt: number) => ({
  hostedId: ID,
  version: 1,
  viewerUrl: `https://portal.example.test/a/${ID}`,
  hash: 'h',
  publishedAt
})

describe('published artifacts', () => {
  afterEach(() => $publishedArtifacts.set({}))

  it('remembers what each local artifact was published as', () => {
    recordPublishedArtifact('s:html:html:dashboard', entry(1))
    expect($publishedArtifacts.get()['s:html:html:dashboard']).toEqual(entry(1))
  })

  it('keeps only the newest 200, so an old one publishes as a new artifact', () => {
    for (let i = 0; i < 201; i++) {
      recordPublishedArtifact(`s:${i}`, entry(i))
    }

    const ids = Object.keys($publishedArtifacts.get())
    expect(ids).toHaveLength(200)
    expect(ids).not.toContain('s:0')
    expect(ids).toContain('s:200')
  })
})

describe('publishedFor', () => {
  const entry = (hash: string, version: number) => ({
    hash,
    hostedId: '01jabcdefghjkmnpqrstvwxyz0',
    publishedAt: 1,
    version,
    viewerUrl: 'https://portal.example.test/a/01jabcdefghjkmnpqrstvwxyz0'
  })

  it('prefers the panel’s own Publish record, else the newest version the agent uploaded', async () => {
    const { publishedFor } = await import('./published-artifacts')

    expect(publishedFor('local', ['h1', 'h2'], { local: entry('h0', 9) }, { h2: entry('h2', 2) })?.version).toBe(9)
    expect(publishedFor('local', ['h1', 'h2'], {}, { h1: entry('h1', 1), h2: entry('h2', 2) })?.version).toBe(2)
    expect(publishedFor('local', ['h1'], {}, {})).toBeUndefined()
  })
})
