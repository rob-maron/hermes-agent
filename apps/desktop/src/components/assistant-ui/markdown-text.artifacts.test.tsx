import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetHostedArtifactsStatus } from '@/lib/local-artifact-publish'
import { artifactsForSession, clearArtifactRegistry } from '@/store/artifacts'
import { $previewTabs } from '@/store/preview'
import { $hostedByContent, $publishedArtifacts } from '@/store/published-artifacts'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'

import { MarkdownTextContent } from './markdown-text'

const HTML_DOC = `<!doctype html>
<html>
<head><title>Pomodoro Timer</title></head>
<body>
<h1>Pomodoro</h1>
<p>A tiny focus timer that counts down twenty-five minutes.</p>
<script>let seconds = 25 * 60; setInterval(() => { seconds -= 1 }, 1000)</script>
</body>
</html>`

const SMALL_SNIPPET = 'const x = 1'

function fenced(language: string, body: string): string {
  return `Here you go:\n\n\`\`\`${language}\n${body}\n\`\`\`\n`
}

// End-to-end for the artifact path: a substantial ```html fence in assistant
// markdown must come out of preprocessMarkdown -> Streamdown -> SyntaxHighlighter
// as an artifact card (registered in the store), while small fences keep the
// plain code-card path.
describe('MarkdownTextContent artifacts', () => {
  beforeEach(() => {
    $activeSessionId.set('session-artifacts')
    $selectedStoredSessionId.set(null)
    window.localStorage.clear()
    clearArtifactRegistry()
  })

  afterEach(() => {
    cleanup()
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    clearArtifactRegistry()
    window.localStorage.clear()
  })

  it('renders a substantial html fence as an artifact card and registers it', async () => {
    render(<MarkdownTextContent isRunning={false} text={fenced('html', HTML_DOC)} />)

    const card = await screen.findByText('Pomodoro Timer')

    expect(card.closest('button')?.dataset.slot).toBe('aui_artifact-card')
    expect(artifactsForSession('session-artifacts')).toHaveLength(1)
    expect(artifactsForSession('session-artifacts')[0]?.kind).toBe('html')
    // Registration alone must not open the rail (offer, don't hijack).
    expect($previewTabs.get()).toHaveLength(0)
  })

  it('keeps a small fence as a plain code block', async () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text={fenced('js', SMALL_SNIPPET)} />)

    // The code card mounts synchronously; Shiki may split tokens into spans,
    // so assert on the card slots rather than text content.
    expect(container.querySelector('[data-slot="code-card"]')).not.toBeNull()
    expect(container.querySelector('[data-slot="aui_artifact-card"]')).toBeNull()
    expect(artifactsForSession('session-artifacts')).toHaveLength(0)
  })

  it('does not register while the message is still streaming', async () => {
    render(<MarkdownTextContent isRunning text={fenced('html', HTML_DOC)} />)

    await screen.findByText('Pomodoro Timer')

    expect(artifactsForSession('session-artifacts')).toHaveLength(0)
  })

  describe('telling the user a page can be shared', () => {
    function desktop(available: boolean) {
      const api = vi.fn(async (request: { path: string }) =>
        request.path === '/api/hosted-artifacts/status'
          ? { available, reason: available ? 'attached' : 'not_logged_in' }
          : undefined
      )

      window.hermesDesktop = { api, openExternal: vi.fn() } as never

      return api
    }

    afterEach(() => {
      resetHostedArtifactsStatus()
      $publishedArtifacts.set({})
      $hostedByContent.set({})
      Reflect.deleteProperty(window, 'hermesDesktop')
    })

    it('puts Share on the card and says only the user can see the page', async () => {
      desktop(true)
      render(<MarkdownTextContent isRunning={false} text={fenced('html', HTML_DOC)} />)

      expect(await screen.findByRole('button', { name: 'Share' })).toBeTruthy()
      expect(await screen.findByText(/Only you can see it/)).toBeTruthy()
      // Opening the page stays the card's main action.
      expect(screen.getByText('Pomodoro Timer').closest('button')?.dataset.slot).toBe('aui_artifact-card')
    })

    it('shows no Share when the account cannot save pages', async () => {
      const api = desktop(false)
      render(<MarkdownTextContent isRunning={false} text={fenced('html', HTML_DOC)} />)

      await screen.findByText('Pomodoro Timer')
      await waitFor(() => expect(api).toHaveBeenCalled())
      expect(screen.queryByRole('button', { name: 'Share' })).toBeNull()
      expect(screen.queryByText(/Only you can see it/)).toBeNull()
    })
  })
})
