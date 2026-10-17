import { useState, useRef, useCallback, useEffect } from 'react'

/**
 * Smooth progress slider with fluid dragging.
 * Local state handles real-time thumb position; saves on release.
 */
export default function ProgressSlider({ value = 0, onChange, disabled = false }) {
  const [localValue, setLocalValue] = useState(value)
  const [dragging, setDragging] = useState(false)
  const [saving, setSaving] = useState(false)
  const trackRef = useRef(null)
  const saveTimeoutRef = useRef(null)

  // Sync from server only when not dragging AND not waiting for save to complete
  useEffect(() => {
    if (!dragging && !saving) setLocalValue(value)
  }, [value, dragging, saving])

  const calcPercent = useCallback((clientX) => {
    if (!trackRef.current) return 0
    const rect = trackRef.current.getBoundingClientRect()
    const ratio = (clientX - rect.left) / rect.width
    return Math.max(0, Math.min(100, Math.round(ratio * 100)))
  }, [])

  const handlePointerDown = useCallback((e) => {
    if (disabled) return
    e.preventDefault()
    setDragging(true)
    const pct = calcPercent(e.clientX)
    setLocalValue(pct)
    trackRef.current?.setPointerCapture(e.pointerId)
  }, [disabled, calcPercent])

  const handlePointerMove = useCallback((e) => {
    if (!dragging) return
    setLocalValue(calcPercent(e.clientX))
  }, [dragging, calcPercent])

  const handlePointerUp = useCallback(() => {
    if (!dragging) return
    setDragging(false)
    // Snap to nearest 5 on release and save
    const snapped = Math.round(localValue / 5) * 5
    setLocalValue(snapped)
    if (onChange) {
      setSaving(true)
      clearTimeout(saveTimeoutRef.current)
      saveTimeoutRef.current = setTimeout(() => {
        onChange(snapped)
        // Allow sync from server after a delay (mutation should complete by then)
        setTimeout(() => setSaving(false), 1500)
      }, 100)
    }
  }, [dragging, localValue, onChange])

  const color = localValue >= 100 ? '#10b981' : localValue >= 50 ? '#0891b2' : '#f59e0b'

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-2">
        <label className="text-sm text-gray-500 dark:text-gray-400">Progress</label>
        <span className={`text-sm font-semibold tabular-nums transition-colors ${
          localValue >= 100 ? 'text-emerald-500' : localValue >= 50 ? 'text-brand-500' : 'text-amber-500'
        }`}>
          {localValue}%
        </span>
      </div>

      <div
        ref={trackRef}
        className={`relative h-5 flex items-center select-none ${disabled ? 'opacity-50' : 'cursor-pointer'}`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
      >
        {/* Track background */}
        <div className="absolute inset-x-0 h-2 bg-gray-200 dark:bg-gray-700 rounded-full" />

        {/* Filled track */}
        <div
          className="absolute left-0 h-2 rounded-full"
          style={{
            width: `${localValue}%`,
            backgroundColor: color,
            transition: dragging ? 'none' : 'width 0.3s ease, background-color 0.3s ease',
          }}
        />

        {/* Thumb */}
        <div
          className="absolute w-5 h-5 rounded-full shadow-lg border-2 border-white dark:border-gray-900"
          style={{
            left: `calc(${localValue}% - 10px)`,
            backgroundColor: color,
            transition: dragging ? 'none' : 'left 0.3s ease, background-color 0.3s ease',
            transform: dragging ? 'scale(1.2)' : 'scale(1)',
          }}
        />
      </div>
    </div>
  )
}
