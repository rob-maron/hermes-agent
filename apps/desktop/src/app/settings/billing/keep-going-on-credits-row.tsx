import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { ExternalLink } from '@/lib/icons'

import { ToggleRow } from '../primitives'

import type { BillingRefusal } from './api'
import { useBillingApi } from './api'
import { BillingRefusalInline } from './inline-feedback'
import { useStepUpFlow } from './use-step-up'

const CAPTION = 'mt-2 flex min-w-0 flex-wrap items-center gap-2 text-[length:var(--conversation-caption-font-size)]'

/**
 * Window plans: "Keep going on credits", the same account setting as the Portal's Billing > Topup
 * switch. Optimistic like the Portal switch.
 *
 * Changing it needs Remote Spending (billing:manage) on this terminal. Without it, the click starts the
 * approval itself: the browser opens, the switch holds the requested position, ONE line under it says
 * what to do, and the change applies on its own once approved. No refuse → verify → click-again loop,
 * and no second Remote Spending prompt elsewhere on the page.
 */
export function KeepGoingOnCreditsRow({ enabled }: { enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.settings.billing.keepGoingOnCredits
  const api = useBillingApi()
  const queryClient = useQueryClient()
  const stepUp = useStepUpFlow({ autoOpen: true })
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const [refusal, setRefusal] = useState<BillingRefusal | null>(null)
  const [approving, setApproving] = useState(false)
  // Approved, yet the change was still refused (e.g. an older backend replayed the pre-approval token).
  const [retryValue, setRetryValue] = useState<boolean | null>(null)
  const checked = optimistic ?? enabled

  const settle = async () => {
    await queryClient.invalidateQueries({ queryKey: ['billing', 'state'] })
    setOptimistic(null)
  }

  const apply = async (next: boolean, afterApproval = false): Promise<void> => {
    const result = await api.updateKeepGoingOnCredits(next)

    if (result.ok) {
      setRetryValue(null)

      return settle()
    }

    if (result.refusal.kind === 'insufficient_scope' && afterApproval) {
      // Never the generic "start a top-up" copy here: the user just approved. Offer a plain retry.
      setRetryValue(next)

      return settle()
    }

    if (result.refusal.kind === 'insufficient_scope') {
      // Needs Remote Spending: approve, then apply the SAME change (the switch keeps the requested position).
      setApproving(true)
      const granted = await stepUp.start()
      setApproving(false)

      if (granted) {
        stepUp.dismiss() // the change itself is the confirmation; no extra "Verification complete" line

        return apply(next, true)
      }

      // Denied / failed / cancelled: the switch goes back; a denial keeps its one-line reason below.
      return settle()
    }

    setRefusal(result.refusal)

    return settle()
  }

  const onChange = (next: boolean) => {
    setOptimistic(next)
    setRefusal(null)
    setRetryValue(null)
    stepUp.dismiss()
    void apply(next)
  }

  let below = <BillingRefusalInline refusal={refusal} />

  if (approving) {
    below = (
      <div className={`${CAPTION} text-(--ui-text-tertiary)`}>
        <span>{copy.approveInBrowser}</span>
        {stepUp.verification?.code && (
          <span className="font-mono text-[0.72rem] font-semibold text-foreground">{stepUp.verification.code}</span>
        )}
        {stepUp.verification && (
          <Button onClick={stepUp.openVerification} size="sm" type="button" variant="outline">
            {copy.openAgain}
            <ExternalLink className="size-3.5" />
          </Button>
        )}
        <Button
          onClick={() => {
            // dismiss() makes the pending start() return early, so put the switch back here.
            stepUp.dismiss()
            setApproving(false)
            void settle()
          }}
          size="sm"
          type="button"
          variant="ghost"
        >
          {copy.cancel}
        </Button>
      </div>
    )
  } else if (retryValue !== null) {
    below = (
      <div className={`${CAPTION} text-(--ui-text-tertiary)`}>
        <span>{copy.approvedRetry}</span>
        <Button
          onClick={() => {
            const next = retryValue
            setRetryValue(null)
            setOptimistic(next)
            void apply(next, true)
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          {copy.tryAgain}
        </Button>
      </div>
    )
  } else if (stepUp.message?.kind === 'error') {
    below = (
      <div className={`${CAPTION} text-(--ui-text-tertiary)`}>
        <span>
          <span className="font-medium text-foreground">{stepUp.message.title}:</span> {stepUp.message.text}
        </span>
        <Button onClick={stepUp.dismiss} size="sm" type="button" variant="ghost">
          {t.settings.billing.stepUp.dismiss}
        </Button>
      </div>
    )
  }

  return (
    <ToggleRow
      below={below}
      checked={checked}
      description={copy.description}
      disabled={optimistic !== null}
      label={copy.title}
      onChange={onChange}
    />
  )
}
