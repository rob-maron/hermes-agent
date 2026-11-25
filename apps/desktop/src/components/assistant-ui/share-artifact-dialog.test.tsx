import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { resetHostedArtifactSharing } from '@/store/hosted-artifact-sharing'

import { ShareArtifactDialog, type ShareSuggestion } from './share-artifact-dialog'

const ID = '01jabcdefghjkmnpqrstvwxyz0'
const VIEWER = `https://portal.example.test/a/${ID}`
const PRIVATE = {
  groups: 0,
  invites: 0,
  members: 0,
  ok: true,
  org: false,
  pin: 'latest',
  share_url: '',
  viewer_url: VIEWER
}

interface ApiRequest {
  path: string
  method?: string
  body?: { artifact_id?: string; org?: boolean }
}

function desktop(shareAnswer: unknown = { ...PRIVATE, org: true }) {
  const api = vi.fn(async (request: ApiRequest) =>
    request.path.startsWith('/api/hosted-artifacts/sharing') ? PRIVATE : shareAnswer
  )
  const openExternal = vi.fn().mockResolvedValue(undefined)
  window.hermesDesktop = { api, openExternal } as never

  return { api, openExternal }
}

const shareCalls = (api: ReturnType<typeof desktop>['api']) =>
  api.mock.calls.map(([request]) => request).filter(request => request.path === '/api/hosted-artifacts/share')

function open(suggestion?: ShareSuggestion) {
  const onOpenChange = vi.fn()
  render(
    <I18nProvider>
      <ShareArtifactDialog
        onOpenChange={onOpenChange}
        open
        suggestion={suggestion}
        target={{ artifactId: ID, title: 'Q3 plan', viewerUrl: VIEWER }}
      />
    </I18nProvider>
  )

  return onOpenChange
}

afterEach(() => {
  cleanup()
  resetHostedArtifactSharing()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('ShareArtifactDialog', () => {
  it('says who can see it now', async () => {
    desktop()
    open()

    expect(await screen.findByText('Only you can see it')).toBeTruthy()
    expect(screen.getByText('Share “Q3 plan”')).toBeTruthy()
  })

  it('the org switch is applied in Hermes as the user’s own click', async () => {
    const { api, openExternal } = desktop()
    const onOpenChange = open()
    await screen.findByText('Only you can see it')

    fireEvent.click(screen.getByRole('switch'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(shareCalls(api)).toEqual([expect.objectContaining({ body: { artifact_id: ID, org: true }, method: 'POST' })])
    expect(openExternal).not.toHaveBeenCalled()
  })

  it('typed people go to the Portal with their addresses filled in, never shared from here', async () => {
    const { api, openExternal } = desktop()
    open()
    await screen.findByText('Only you can see it')

    fireEvent.change(screen.getByPlaceholderText('Add emails, separated by commas'), {
      target: { value: 'Dana@Example.com, eli@example.org' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save and invite on the Portal' }))

    await waitFor(() =>
      expect(openExternal).toHaveBeenCalledWith(`${VIEWER}?share=1&invite=dana%40example.com%2Celi%40example.org`)
    )
    expect(shareCalls(api)).toEqual([])
  })

  it('refuses something that is not an address, and sends nothing', async () => {
    const { api, openExternal } = desktop()
    open()
    await screen.findByText('Only you can see it')

    fireEvent.change(screen.getByPlaceholderText('Add emails, separated by commas'), { target: { value: 'dana' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Not an email address: dana')
    expect(shareCalls(api)).toEqual([])
    expect(openExternal).not.toHaveBeenCalled()
  })

  it('opens with what the agent asked for, applied only on Save', async () => {
    const { api } = desktop()
    open({ emails: ['dana@example.com'], org: true })
    await screen.findByText('Only you can see it')

    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true')
    expect((screen.getByPlaceholderText('Add emails, separated by commas') as HTMLInputElement).value).toBe(
      'dana@example.com'
    )
    expect(screen.getByText('Filled in from your request. Nothing changes until you save.')).toBeTruthy()
    expect(shareCalls(api)).toEqual([])
  })

  it('a refused change stays open with the reason', async () => {
    desktop({ error: 'Hosted artifacts are not available for this account.', ok: false })
    const onOpenChange = open({ org: true })
    await screen.findByText('Only you can see it')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'Hosted artifacts are not available for this account.'
    )
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })
})

describe('ShareArtifactDialog for a page not saved yet', () => {
  function openUnsaved(saveFirst: () => Promise<{ artifactId: string; title: string; viewerUrl: string }>) {
    const onOpenChange = vi.fn()
    render(
      <I18nProvider>
        <ShareArtifactDialog onOpenChange={onOpenChange} open saveFirst={saveFirst} target={null} title="Draft" />
      </I18nProvider>
    )

    return onOpenChange
  }

  it('says Save saves it first, and saves nothing when nothing was chosen', async () => {
    desktop()
    const saveFirst = vi.fn()
    const onOpenChange = openUnsaved(saveFirst)

    expect(screen.getByText('Sharing saves this page to your Nous account first.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'More options on the Portal' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(saveFirst).not.toHaveBeenCalled()
  })

  it('typed people: saves the page, then opens the Portal with them filled in', async () => {
    const { api, openExternal } = desktop()
    const saveFirst = vi.fn().mockResolvedValue({ artifactId: ID, title: 'Draft', viewerUrl: VIEWER })
    openUnsaved(saveFirst)

    fireEvent.change(screen.getByPlaceholderText('Add emails, separated by commas'), {
      target: { value: 'a@example.com' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save and invite on the Portal' }))

    await waitFor(() => expect(openExternal).toHaveBeenCalledWith(`${VIEWER}?share=1&invite=a%40example.com`))
    expect(saveFirst).toHaveBeenCalledTimes(1)
    // A page saved just now is private: no org change to send.
    expect(shareCalls(api)).toEqual([])
  })
})
