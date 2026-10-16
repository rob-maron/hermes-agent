import type { BillingBlock } from '@hermes/shared/billing'
import type { UsageLimit } from '@hermes/shared/plan-usage'

import { t } from '../i18n/runtime.js'

export interface BillingDialogCopy {
  cancelLabel: string
  confirmLabel: string
  detail: string
  title: string
}

/**
 * Copy for the out-of-credits confirm dialog (the TUI's billing wall). The
 * dialog is the actionable layer — the full provider guidance already lands in
 * the transcript — so `detail` stays to one concise, non-truncating line and the
 * confirm button carries the recovery: Nous → `/topup`, other providers → their
 * billing page (or `/model` to switch when we have no URL). Pure + exported so
 * the wording is unit-tested without driving the gateway.
 */
export function billingDialogCopy(block: BillingBlock): BillingDialogCopy {
  if (block.is_nous) {
    return {
      cancelLabel: t('libText.billingDialog.dismiss'),
      confirmLabel: t('libText.billingDialog.topUp'),
      detail: t('libText.billingDialog.nousDetail'),
      title: t('libText.billingDialog.nousTitle')
    }
  }

  const label = block.provider_label || t('libText.billingDialog.yourProvider')

  return {
    cancelLabel: t('libText.billingDialog.dismiss'),
    confirmLabel: block.billing_url
      ? t('libText.billingDialog.openBillingPage')
      : t('libText.billingDialog.switchProvider'),
    detail: t('libText.billingDialog.providerDetail', label),
    title: t('libText.billingDialog.providerTitle', label)
  }
}

export interface UsageLimitDialog extends BillingDialogCopy {
  /** `config.set model` value for the switch (this session only), or null: open /topup instead. */
  switchValue: null | string
}

/**
 * A usage-window plan's limit (error_surface.usage_limit, agent/plan_limits.py): the confirm button
 * switches THIS session to the plan's recommended model and re-sends the message; the detail names the
 * other way on (credits, via /topup). With no model to offer, the confirm opens /topup instead. Never
 * switches on its own: the dialog is the ask.
 */
export function usageLimitDialog(limit: UsageLimit): UsageLimitDialog {
  const suggestion = limit.suggestions?.[0]
  const sentence = (limit.message || '').trim()

  const credits =
    limit.credits?.action === 'use_credits'
      ? 'Or turn on Keep going in /topup to use your credits.'
      : limit.credits?.action === 'add_credits'
        ? 'Or add credits with /topup.'
        : ''

  if (!suggestion) {
    return {
      cancelLabel: 'Dismiss',
      confirmLabel: 'Open /topup',
      switchValue: null,
      detail: [sentence, credits].filter(Boolean).join('\n'),
      title: 'Limit reached'
    }
  }

  const note = suggestion.note ? ` (${suggestion.note})` : ''

  return {
    cancelLabel: 'Dismiss',
    confirmLabel: `Switch to ${suggestion.label}${note}`,
    detail: [sentence, credits].filter(Boolean).join('\n'),
    switchValue: `${suggestion.model} --provider ${suggestion.provider || 'nous'} --session`,
    title: 'Limit reached'
  }
}
