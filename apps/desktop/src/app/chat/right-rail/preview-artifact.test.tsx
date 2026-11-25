import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { artifactContentHash } from '@/lib/artifact-detect'
import { $artifactRegistry, $artifactVersionSelection, artifactPreviewTarget, upsertArtifact } from '@/store/artifacts'
import { $hostedByContent, $publishedArtifacts } from '@/store/published-artifacts'

import { ArtifactPreview, resetHostedArtifactsStatus } from './preview-artifact'

function register(title: string, kind: 'code' | 'html' | 'svg', content: string) {
  const result = upsertArtifact('session-1', { kind, language: kind === 'code' ? 'python' : kind, title }, content)

  if (!result) {
    throw new Error('artifact did not register')
  }

  return result
}

async function renderArtifact(artifactId: string) {
  const record = $artifactRegistry.get()['session-1']!.find(item => item.id === artifactId)!

  await act(async () => {
    render(<ArtifactPreview target={artifactPreviewTarget(record)} />)
  })
}

describe('ArtifactPreview', () => {
  afterEach(() => {
    cleanup()
    $artifactRegistry.set({})
    $artifactVersionSelection.set({})
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('renders html in a scripts-only sandboxed frame the parent app is unreachable from', async () => {
    const { artifactId } = register('Dashboard', 'html', '<h1>Hi</h1>')
    await renderArtifact(artifactId)

    const frame = screen.getByTitle('Dashboard') as HTMLIFrameElement

    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame.srcdoc).toContain('<h1>Hi</h1>')
    // No allow-same-origin: scripts inside cannot reach the renderer's origin.
    expect(frame.getAttribute('sandbox')).not.toContain('same-origin')
  })

  it('opens a browser-hosted HTML blob without corrupting it as a file URL', async () => {
    const openPreviewInBrowser = vi.fn().mockResolvedValue(undefined)
    window.hermesDesktop = {
      openExternal: vi.fn().mockResolvedValue(undefined),
      openPreviewInBrowser,
      saveImageBuffer: vi.fn().mockResolvedValue('blob:http://localhost/artifact')
    } as never
    const { artifactId } = register('Dashboard', 'html', '<h1>Hi</h1>')
    await renderArtifact(artifactId)

    fireEvent.click(screen.getByRole('button', { name: /open in browser/i }))

    await waitFor(() => {
      expect(openPreviewInBrowser).toHaveBeenCalledWith('blob:http://localhost/artifact')
    })
  })

  it('strips scripts out of svg before it renders inline', async () => {
    const { artifactId } = register(
      'Logo',
      'svg',
      '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    )

    await renderArtifact(artifactId)

    expect(document.querySelector('svg')).not.toBeNull()
    expect(document.querySelector('svg script')).toBeNull()
  })

  it('offers only the source view for code, which has nothing to render', async () => {
    const { artifactId } = register('Solver', 'code', 'print("hi")')
    await renderArtifact(artifactId)

    expect(screen.queryByRole('button', { name: /rendered/i })).toBeNull()
  })

  it('shows the version stepper once an artifact has history, and follows the selection', async () => {
    register('Dashboard', 'html', '<h1>v1</h1>')
    const { artifactId } = register('Dashboard', 'html', '<h1>v2</h1>')
    await renderArtifact(artifactId)

    expect(screen.getByText('v2 of 2')).toBeTruthy()
    expect((screen.getByTitle('Dashboard') as HTMLIFrameElement).srcdoc).toContain('v2')

    await act(async () => {
      $artifactVersionSelection.set({ [artifactId]: 0 })
    })

    expect(screen.getByText('v1 of 2')).toBeTruthy()
    expect((screen.getByTitle('Dashboard') as HTMLIFrameElement).srcdoc).toContain('v1')
  })

  it('hides the stepper for a single-version artifact', async () => {
    const { artifactId } = register('Dashboard', 'html', '<h1>only</h1>')
    await renderArtifact(artifactId)

    expect(screen.queryByText('v1 of 1')).toBeNull()
  })

  it('picks up a new version in an already-open tab', async () => {
    const { artifactId } = register('Dashboard', 'html', '<h1>v1</h1>')
    await renderArtifact(artifactId)

    await act(async () => {
      register('Dashboard', 'html', '<h1>v2</h1>')
    })

    expect((screen.getByTitle('Dashboard') as HTMLIFrameElement).srcdoc).toContain('v2')
  })

  it('falls back to an empty state when the registry no longer has the artifact', async () => {
    const { artifactId } = register('Dashboard', 'html', '<h1>gone</h1>')
    const record = $artifactRegistry.get()['session-1']!.find(item => item.id === artifactId)!
    const target = artifactPreviewTarget(record)

    $artifactRegistry.set({})

    await act(async () => {
      render(<ArtifactPreview target={target} />)
    })

    expect(screen.queryByTitle('Dashboard')).toBeNull()
  })

  it('gives the missing-artifact state a Close that closes its tab', async () => {
    const { artifactId } = register('Dashboard', 'html', '<h1>gone</h1>')
    const record = $artifactRegistry.get()['session-1']!.find(item => item.id === artifactId)!
    const target = artifactPreviewTarget(record)
    const onClose = vi.fn()

    $artifactRegistry.set({})

    await act(async () => {
      render(<ArtifactPreview onClose={onClose} target={target} />)
    })

    expect(screen.getByText('Artifact unavailable')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledOnce()
  })
})

describe('ArtifactPreview Share (hosted artifacts)', () => {
  const HOSTED_ID = '01jabcdefghjkmnpqrstvwxyz0'
  const VIEWER = `https://portal.example.test/a/${HOSTED_ID}`

  afterEach(() => {
    cleanup()
    $artifactRegistry.set({})
    $artifactVersionSelection.set({})
    $publishedArtifacts.set({})
    $hostedByContent.set({})
    resetHostedArtifactsStatus()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  interface ApiRequest {
    path: string
    method?: string
    body?: { title?: string; html?: string; artifact_id?: string; base_version?: number; org?: boolean }
  }

  /** A desktop bridge whose backend answers the status read and the publish. */
  function desktop(available: boolean, publish: unknown = undefined) {
    const api = vi.fn(async (request: ApiRequest) =>
      request.path === '/api/hosted-artifacts/status'
        ? { available, reason: available ? 'attached' : 'not_logged_in' }
        : publish
    )

    const openExternal = vi.fn().mockResolvedValue(undefined)
    window.hermesDesktop = { api, openExternal } as never

    return { api, openExternal }
  }

  const publishCall = (api: ReturnType<typeof desktop>['api']): ApiRequest | undefined =>
    api.mock.calls.map(([request]) => request).find(request => request.path === '/api/hosted-artifacts/publish')

  it('shows no Share button unless the account may save pages', async () => {
    const { api } = desktop(false)
    const { artifactId } = register('Dashboard', 'html', '<h1>Hi</h1>')
    await renderArtifact(artifactId)

    await waitFor(() => expect(api).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Share' })).toBeNull()
  })

  it('offers Share only for HTML artifacts', async () => {
    desktop(true)
    const { artifactId } = register('Logo', 'svg', '<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    await renderArtifact(artifactId)

    expect(screen.queryByRole('button', { name: 'Share' })).toBeNull()
  })

  it('uploads nothing until the user acts; Copy link saves the page privately first, then copies its link', async () => {
    const { api, openExternal } = desktop(true, { ok: true, id: HOSTED_ID, version: 1, viewer_url: VIEWER })
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const { artifactId } = register('Dashboard', 'html', '<h1>Hi</h1>')
    await renderArtifact(artifactId)

    // One Share entry point, and no separate save button: opening the panel uploads nothing.
    expect(screen.queryByRole('button', { name: 'Save page' })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: 'Share' }))
    expect(await screen.findByText('Sharing saves this page to your Nous account first.')).toBeTruthy()
    expect(publishCall(api)).toBeUndefined()

    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(VIEWER))
    const request = publishCall(api)!
    expect(request.method).toBe('POST')
    expect(request.body?.title).toBe('Dashboard')
    expect(request.body?.html).toMatch(/^<!doctype html>/i)
    expect(request.body?.html).toContain('<h1>Hi</h1>')
    expect(request.body?.artifact_id).toBeUndefined()
    expect($publishedArtifacts.get()[artifactId]).toMatchObject({ hostedId: HOSTED_ID, version: 1, viewerUrl: VIEWER })
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeTruthy()
    expect(openExternal).not.toHaveBeenCalled()
    // Unchanged since: no update to save, and copying again uploads nothing more.
    expect(screen.queryByRole('button', { name: 'Save update' })).toBeNull()
    expect(api.mock.calls.filter(([r]) => r.path === '/api/hosted-artifacts/publish')).toHaveLength(1)
  })

  it('saves a changed page as a new version of the same hosted artifact, on Save update', async () => {
    const { api } = desktop(true, { ok: true, id: HOSTED_ID, version: 2, viewer_url: VIEWER })
    const { artifactId } = register('Dashboard', 'html', '<h1>Two</h1>')
    $publishedArtifacts.set({
      [artifactId]: { hostedId: HOSTED_ID, version: 1, viewerUrl: VIEWER, hash: 'an-older-version', publishedAt: 1 }
    })
    await renderArtifact(artifactId)

    fireEvent.click(await screen.findByRole('button', { name: 'Save update' }))

    await waitFor(() => expect(publishCall(api)).toBeTruthy())
    expect(publishCall(api)?.body).toMatchObject({ artifact_id: HOSTED_ID, base_version: 1 })
    await waitFor(() => expect($publishedArtifacts.get()[artifactId]?.version).toBe(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save update' })).toBeNull())
  })

  it('a refused save remembers nothing and opens nothing', async () => {
    const { openExternal } = desktop(true, { ok: false, error: 'Hosted artifacts are not available for this account.' })
    const { artifactId } = register('Dashboard', 'html', '<h1>Hi</h1>')
    await renderArtifact(artifactId)

    fireEvent.click(await screen.findByRole('button', { name: 'Share' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Copy link' }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect($publishedArtifacts.get()).toEqual({})
    expect(openExternal).not.toHaveBeenCalled()
  })

  it('Share is always there; for a page not saved yet, Save saves it first, then shares', async () => {
    const calls: ApiRequest[] = []

    const api = vi.fn(async (request: ApiRequest) => {
      calls.push(request)

      if (request.path === '/api/hosted-artifacts/status') {
        return { available: true, reason: 'attached' }
      }

      if (request.path === '/api/hosted-artifacts/publish') {
        return { ok: true, id: HOSTED_ID, version: 1, viewer_url: VIEWER }
      }

      return { ok: true }
    })

    const openExternal = vi.fn().mockResolvedValue(undefined)
    window.hermesDesktop = { api, openExternal } as never
    const { artifactId } = register('Dashboard', 'html', '<h1>Hi</h1>')
    await renderArtifact(artifactId)

    fireEvent.click(await screen.findByRole('button', { name: 'Share' }))
    expect(await screen.findByText('Sharing saves this page to your Nous account first.')).toBeTruthy()
    fireEvent.click(screen.getByRole('switch'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(calls.map(c => c.path)).toContain('/api/hosted-artifacts/share'))
    const paths = calls.map(c => c.path)
    expect(paths.indexOf('/api/hosted-artifacts/publish')).toBeLessThan(paths.indexOf('/api/hosted-artifacts/share'))
    expect(calls.find(c => c.path === '/api/hosted-artifacts/share')?.body).toEqual({
      artifact_id: HOSTED_ID,
      org: true
    })
    expect($publishedArtifacts.get()[artifactId]).toMatchObject({ hostedId: HOSTED_ID })
    // Sharing does not open the page in the browser; only typed people would open the Portal.
    expect(openExternal).not.toHaveBeenCalled()
  })

  it('a page the agent already uploaded counts as published: no second copy', async () => {
    const { api } = desktop(true)
    const { artifactId } = register('Dashboard', 'html', '<h1>Hi</h1>')
    $hostedByContent.set({
      [artifactContentHash('<h1>Hi</h1>')]: {
        hash: artifactContentHash('<h1>Hi</h1>'),
        hostedId: HOSTED_ID,
        publishedAt: 1,
        version: 3,
        viewerUrl: VIEWER
      }
    })
    await renderArtifact(artifactId)

    fireEvent.click(await screen.findByRole('button', { name: 'Share' }))
    // The panel treats it as saved (who can see it, More options), not as a page to save first.
    expect(await screen.findByRole('button', { name: 'More options on the Portal' })).toBeTruthy()
    expect(screen.queryByText('Sharing saves this page to your Nous account first.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save update' })).toBeNull()
    expect(publishCall(api)).toBeUndefined()
  })
})
