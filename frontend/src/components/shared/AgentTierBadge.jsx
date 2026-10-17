import { cn } from '../../utils/cn'

/**
 * Model tier badge for agents and users.
 *
 * Tiers:
 *   claude_code - Anthropic Claude Code (blue)
 *   codex       - OpenAI Codex (violet)
 *   shizuha     - Shizuha-native GLM/Qwen-based agent (teal)
 *   human       - Human operator (amber)
 *
 * Props:
 *   tier        - 'claude_code' | 'codex' | 'shizuha' | 'human'
 *   show_label  - render text label alongside icon (default false)
 *   size        - 'sm' (default) | 'md'
 *   className
 */
export default function AgentTierBadge({ tier = 'human', show_label = false, size = 'sm', className }) {
  const config = TIER_CONFIG[tier] ?? TIER_CONFIG.human

  const textSize = size === 'md' ? 'text-xs' : 'text-[0.65rem]'
  const padding  = size === 'md' ? 'px-2 py-0.5' : 'px-1.5 py-px'

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded font-semibold leading-none whitespace-nowrap',
        padding,
        textSize,
        config.classes,
        className,
      )}
      title={config.label}
      aria-label={`Tier: ${config.label}`}
    >
      <span aria-hidden>{config.icon}</span>
      {show_label && <span>{config.label}</span>}
    </span>
  )
}

const TIER_CONFIG = {
  claude_code: {
    label:   'Claude Code',
    icon:    '⬡',
    classes: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  },
  codex: {
    label:   'Codex',
    icon:    '◈',
    classes: 'bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-300',
  },
  shizuha: {
    label:   'Shizuha',
    icon:    '⬢',
    classes: 'bg-teal-100 text-teal-800 dark:bg-teal-900/40 dark:text-teal-300',
  },
  human: {
    label:   'Human',
    icon:    '◎',
    classes: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  },
}
