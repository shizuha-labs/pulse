/**
 * WorkflowDiagram — Visual workflow designer/viewer.
 *
 * Renders statuses as interactive nodes and transitions as SVG arrows.
 * Supports both viewing and editing (add/remove transitions, click to inspect).
 * Inspired by Jira's workflow designer and ServiceNow's flow designer.
 */

import { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import {
  Plus, Trash2, ArrowRight, Users, Shield, UserCheck,
  Zap, User, Bot, Globe, ChevronDown, X, Edit2,
  GitBranch, Eye, Pencil, MousePointer,
} from 'lucide-react'
import { cn } from '../utils/cn'

// ── Layout Constants ──

const NODE_W = 180
const NODE_H = 64
const GAP_X = 60
const GAP_Y = 80
const PAD = 50

// ── Category Colors ──

const CATEGORY_COLORS = {
  todo:        { bg: '#f3f4f6', border: '#9ca3af', text: '#374151', fill: '#f9fafb', dark: { bg: '#1f2937', border: '#4b5563', text: '#d1d5db', fill: '#111827' } },
  open:        { bg: '#f3f4f6', border: '#9ca3af', text: '#374151', fill: '#f9fafb', dark: { bg: '#1f2937', border: '#4b5563', text: '#d1d5db', fill: '#111827' } },
  in_progress: { bg: '#dbeafe', border: '#3b82f6', text: '#1e40af', fill: '#eff6ff', dark: { bg: '#1e3a5f', border: '#3b82f6', text: '#93c5fd', fill: '#172554' } },
  done:        { bg: '#dcfce7', border: '#22c55e', text: '#166534', fill: '#f0fdf4', dark: { bg: '#14532d', border: '#22c55e', text: '#86efac', fill: '#052e16' } },
  cancelled:   { bg: '#f3f4f6', border: '#6b7280', text: '#6b7280', fill: '#fafafa', dark: { bg: '#1f2937', border: '#4b5563', text: '#9ca3af', fill: '#111827' } },
  // Scheduled: violet tone mirrors WorkflowStatusBar pill, signals "timer-owned"
  scheduled:   { bg: '#ede9fe', border: '#a855f7', text: '#6b21a8', fill: '#f5f3ff', dark: { bg: '#3b0764', border: '#a855f7', text: '#d8b4fe', fill: '#2e1065' } },
}

// ── Layout Engine ──

function layoutNodes(statuses, initialStatus) {
  const categoryOrder = ['todo', 'open', 'in_progress', 'done', 'scheduled', 'cancelled']
  const groups = {}
  for (const cat of categoryOrder) groups[cat] = []

  for (const s of statuses) {
    const cat = s.category || 'todo'
    if (!groups[cat]) groups[cat] = []
    groups[cat].push(s)
  }

  // Put initial status first
  for (const cat of categoryOrder) {
    if (!groups[cat]) continue
    const idx = groups[cat].findIndex(s => s.name === initialStatus)
    if (idx > 0) {
      const [item] = groups[cat].splice(idx, 1)
      groups[cat].unshift(item)
    }
  }

  const positions = new Map()
  let col = 0

  for (const cat of categoryOrder) {
    const items = groups[cat] || []
    if (items.length === 0) continue

    for (let row = 0; row < items.length; row++) {
      positions.set(items[row].name, {
        x: PAD + col * (NODE_W + GAP_X),
        y: PAD + row * (NODE_H + GAP_Y),
        w: NODE_W,
        h: NODE_H,
        status: items[row],
      })
    }
    col++
  }

  return positions
}

// ── Arrow Path ──

function getArrowPath(fromNode, toNode, offset = 0) {
  if (!fromNode || !toNode) return ''

  const fromCx = fromNode.x + fromNode.w / 2
  const fromCy = fromNode.y + fromNode.h / 2
  const toCx = toNode.x + toNode.w / 2
  const toCy = toNode.y + toNode.h / 2

  let sx, sy, ex, ey

  // Self-loop
  if (fromNode.status.name === toNode.status.name) {
    const r = 30
    sx = fromCx
    sy = fromNode.y
    return `M ${sx},${sy} C ${sx - r},${sy - r * 2.5} ${sx + r},${sy - r * 2.5} ${sx},${sy}`
  }

  if (toCx > fromCx + fromNode.w * 0.3) {
    sx = fromNode.x + fromNode.w
    sy = fromCy + offset
    ex = toNode.x
    ey = toCy + offset
  } else if (toCx < fromCx - fromNode.w * 0.3) {
    sx = fromNode.x
    sy = fromCy + offset
    ex = toNode.x + toNode.w
    ey = toCy + offset
  } else if (toCy > fromCy) {
    sx = fromCx + offset
    sy = fromNode.y + fromNode.h
    ex = toCx + offset
    ey = toNode.y
  } else {
    sx = fromCx + offset
    sy = fromNode.y
    ex = toCx + offset
    ey = toNode.y + toNode.h
  }

  const dx = ex - sx
  const dy = ey - sy
  const cx1 = sx + dx * 0.4
  const cy1 = sy
  const cx2 = sx + dx * 0.6
  const cy2 = ey

  return `M ${sx},${sy} C ${cx1},${cy1} ${cx2},${cy2} ${ex},${ey}`
}

// ── Main Component ──

export default function WorkflowDiagram({
  statuses = [],
  transitions = [],
  initialStatus = '',
  currentStatus = '',
  isEditing = false,
  onAddTransition,
  onRemoveTransition,
  onSelectStatus,
  onSelectTransition,
  selectedStatus = null,
  selectedTransition = null,
  className = '',
}) {
  const svgRef = useRef(null)
  const [hoveredTransition, setHoveredTransition] = useState(null)
  const [hoveredNode, setHoveredNode] = useState(null)
  const [connectingFrom, setConnectingFrom] = useState(null) // source node for new transition
  const [mousePos, setMousePos] = useState({ x: 0, y: 0 })

  // Layout
  const nodeMap = useMemo(() => layoutNodes(statuses, initialStatus), [statuses, initialStatus])

  // SVG dimensions
  const svgWidth = useMemo(() => {
    let max = 400
    for (const [, n] of nodeMap) max = Math.max(max, n.x + n.w)
    return max + PAD * 2
  }, [nodeMap])

  const svgHeight = useMemo(() => {
    let max = 200
    for (const [, n] of nodeMap) max = Math.max(max, n.y + n.h)
    return max + PAD * 2
  }, [nodeMap])

  // Count parallel edges for offset
  const edgeOffsets = useMemo(() => {
    const counts = new Map()
    const indices = new Map()
    for (const t of transitions) {
      const key = [t.from_status, t.to_status].sort().join('|')
      counts.set(key, (counts.get(key) || 0) + 1)
    }
    const result = new Map()
    for (const t of transitions) {
      const key = [t.from_status, t.to_status].sort().join('|')
      const total = counts.get(key) || 1
      const idx = indices.get(key) || 0
      indices.set(key, idx + 1)
      const offset = total > 1 ? (idx - (total - 1) / 2) * 14 : 0
      result.set(t.id || `${t.from_status}-${t.to_status}`, offset)
    }
    return result
  }, [transitions])

  // ESC to cancel connecting
  useEffect(() => {
    const handler = (e) => { if (e.key === 'Escape') setConnectingFrom(null) }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  // Determine dark mode
  const isDark = document.documentElement.classList.contains('dark')

  return (
    <div className={cn('relative overflow-auto bg-gray-50 dark:bg-gray-900/50 rounded-xl border border-gray-200 dark:border-gray-700', className)}>
      {/* Legend + mode indicator */}
      <div className="absolute top-3 left-3 z-10 flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400 bg-white/80 dark:bg-gray-800/80 backdrop-blur-sm rounded-lg px-3 py-1.5 border border-gray-200 dark:border-gray-700">
        <span className="flex items-center gap-1">
          <span className="w-3 h-3 rounded border-2 border-indigo-500 inline-block" /> Initial
        </span>
        <span className="flex items-center gap-1">
          <span className="w-6 border-t-2 border-gray-400 inline-block" /> Transition
        </span>
        {connectingFrom ? (
          <span className="text-indigo-500 font-medium animate-pulse">
            Click target node to create transition — or ESC to cancel
          </span>
        ) : isEditing ? (
          <span className="text-gray-500">Click a node to inspect &middot; Double-click to start connecting</span>
        ) : null}
      </div>

      {/* Add transition button */}
      {isEditing && !connectingFrom && (
        <div className="absolute top-3 right-3 z-10">
          <button
            onClick={() => setConnectingFrom('__pick_source__')}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-xs font-medium text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 transition-colors shadow-sm"
          >
            <Plus size={14} />
            Add Transition
          </button>
        </div>
      )}
      {connectingFrom && (
        <div className="absolute top-3 right-3 z-10">
          <button
            onClick={() => setConnectingFrom(null)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 rounded-lg text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-100 transition-colors shadow-sm"
          >
            <X size={14} />
            Cancel
          </button>
        </div>
      )}

      <svg
        ref={svgRef}
        width={svgWidth}
        height={svgHeight}
        className="min-w-full"
        style={{ minHeight: Math.max(svgHeight, 300) }}
      >
        <defs>
          <marker id="wf-arrow" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#6b7280" />
          </marker>
          <marker id="wf-arrow-blue" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#3b82f6" />
          </marker>
          <marker id="wf-arrow-hover" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#6366f1" />
          </marker>
          <marker id="wf-arrow-selected" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#8b5cf6" />
          </marker>
          {/* Drop shadow filter */}
          <filter id="node-shadow" x="-10%" y="-10%" width="120%" height="130%">
            <feDropShadow dx="0" dy="2" stdDeviation="3" floodColor="#000" floodOpacity="0.1" />
          </filter>
          <filter id="node-shadow-selected" x="-10%" y="-10%" width="120%" height="130%">
            <feDropShadow dx="0" dy="2" stdDeviation="6" floodColor="#6366f1" floodOpacity="0.3" />
          </filter>
        </defs>

        {/* Category group labels */}
        {(() => {
          const categoryOrder = ['todo', 'open', 'in_progress', 'done', 'scheduled', 'cancelled']
          const categoryLabels = { todo: 'TO DO', open: 'OPEN', in_progress: 'IN PROGRESS', done: 'DONE', cancelled: 'CANCELLED' }
          const seen = new Set()
          const labels = []
          for (const [, node] of nodeMap) {
            const cat = node.status.category || 'todo'
            if (!seen.has(cat)) {
              seen.add(cat)
              labels.push({ cat, x: node.x, y: 15 })
            }
          }
          return labels.map(({ cat, x }) => (
            <text key={cat} x={x + NODE_W / 2} y={20} textAnchor="middle"
              className="text-[10px] font-bold fill-gray-400 dark:fill-gray-600 uppercase tracking-widest"
              style={{ fontSize: 10, letterSpacing: '0.1em' }}>
              {categoryLabels[cat] || cat.toUpperCase()}
            </text>
          ))
        })()}

        {/* Transition arrows */}
        {transitions.map((t, i) => {
          const from = nodeMap.get(t.from_status)
          const to = nodeMap.get(t.to_status)
          if (!from || !to) return null

          const tId = t.id || `${t.from_status}-${t.to_status}-${i}`
          const offset = edgeOffsets.get(tId) || 0
          const path = getArrowPath(from, to, offset)
          const isHovered = hoveredTransition === tId
          const isSelected = selectedTransition?.id === t.id

          // Dim if a node is selected and this transition isn't connected
          const isDimmed = selectedStatus && t.from_status !== selectedStatus && t.to_status !== selectedStatus

          return (
            <g key={tId}
              onMouseEnter={() => setHoveredTransition(tId)}
              onMouseLeave={() => setHoveredTransition(null)}
              onClick={() => onSelectTransition?.(t)}
              className={isEditing ? 'cursor-pointer' : ''}
            >
              {/* Wide hit area */}
              <path d={path} fill="none" stroke="transparent" strokeWidth={18} />
              {/* Visible arrow */}
              <path
                d={path}
                fill="none"
                stroke={isSelected ? '#8b5cf6' : isHovered ? '#6366f1' : '#9ca3af'}
                strokeWidth={isSelected ? 2.5 : isHovered ? 2 : 1.5}
                strokeDasharray={t.from_status === t.to_status ? '4,3' : undefined}
                markerEnd={isSelected ? 'url(#wf-arrow-selected)' : isHovered ? 'url(#wf-arrow-hover)' : 'url(#wf-arrow)'}
                opacity={isDimmed ? 0.15 : 1}
                className="transition-all duration-150"
              />
              {/* Transition label on hover */}
              {(isHovered || isSelected) && from.status.name !== to.status.name && (
                <foreignObject
                  x={(from.x + from.w / 2 + to.x + to.w / 2) / 2 - 80}
                  y={(from.y + from.h / 2 + to.y + to.h / 2) / 2 - 14 + offset}
                  width={160} height={28}
                >
                  <div className="flex items-center justify-center">
                    <span className={cn(
                      'text-[10px] px-2 py-0.5 rounded-full font-medium shadow-sm',
                      isSelected
                        ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300'
                        : 'bg-white text-gray-700 dark:bg-gray-800 dark:text-gray-300 border border-gray-200 dark:border-gray-600'
                    )}>
                      {t.name || `${t.from_status} → ${t.to_status}`}
                    </span>
                  </div>
                </foreignObject>
              )}
            </g>
          )
        })}

        {/* Status nodes */}
        {Array.from(nodeMap.entries()).map(([name, node]) => {
          const colors = CATEGORY_COLORS[node.status.category] || CATEGORY_COLORS.todo
          const c = isDark ? colors.dark : colors
          const isInitial = name === initialStatus
          const isSelected = selectedStatus === name
          // Compare case-insensitively — callers may pass a slug ('scheduled')
          // while node names vary in source (slug vs display). Without
          // normalization the current-status ring never renders.
          const isCurrent = currentStatus
            && String(currentStatus).toLowerCase() === String(name).toLowerCase()
          const isConnectSource = connectingFrom === name
          const isHovered = hoveredNode === name
          const isDimmed = selectedStatus && !isSelected
            && !transitions.some(t => (t.from_status === selectedStatus && t.to_status === name) || (t.to_status === selectedStatus && t.from_status === name))

          return (
            <g key={name}
              onMouseEnter={() => setHoveredNode(name)}
              onMouseLeave={() => setHoveredNode(null)}
              onClick={() => {
                if (connectingFrom === '__pick_source__') {
                  // First click: pick source
                  setConnectingFrom(name)
                } else if (connectingFrom && connectingFrom !== '__pick_source__') {
                  // Second click: pick target → create transition
                  onAddTransition?.(connectingFrom, name)
                  setConnectingFrom(null)
                } else {
                  onSelectStatus?.(isSelected ? null : name)
                }
              }}
              onDoubleClick={() => {
                if (isEditing) setConnectingFrom(name)
              }}
              className={connectingFrom ? 'cursor-crosshair' : (isEditing || onSelectStatus ? 'cursor-pointer' : '')}
            >
              {/* Current-status glow — rendered behind the node as a
                  second rect so the ring is visible even when the node has
                  its own category color fill. */}
              {isCurrent && (
                <rect
                  x={node.x - 4} y={node.y - 4}
                  width={node.w + 8} height={node.h + 8}
                  rx={14} ry={14}
                  fill="none"
                  stroke={c.border || '#a855f7'}
                  strokeWidth={3}
                  className="animate-pulse"
                  opacity={0.75}
                />
              )}
              {/* Node background */}
              <rect
                x={node.x} y={node.y}
                width={node.w} height={node.h}
                rx={12} ry={12}
                fill={c.fill || c.bg}
                stroke={
                  isConnectSource ? '#22c55e'
                  : isCurrent ? (c.border || '#a855f7')
                  : isSelected ? '#6366f1'
                  : isInitial ? '#6366f1'
                  : c.border
                }
                strokeWidth={
                  isConnectSource ? 3
                  : isCurrent ? 3
                  : isSelected ? 2.5
                  : isInitial ? 2
                  : 1
                }
                strokeDasharray={isConnectSource ? '6,3' : undefined}
                filter={(isSelected || isCurrent) ? 'url(#node-shadow-selected)' : 'url(#node-shadow)'}
                opacity={isDimmed ? 0.25 : 1}
                className="transition-all duration-150"
              />
              {/* Status label */}
              <text
                x={node.x + node.w / 2}
                y={node.y + node.h / 2 - 4}
                textAnchor="middle"
                dominantBaseline="middle"
                fill={isDimmed ? '#9ca3af' : (c.text || '#374151')}
                className="text-sm font-semibold pointer-events-none"
                style={{ fontSize: 13, fontWeight: 600 }}
              >
                {node.status.label || node.status.name}
              </text>
              {/* Category subtitle */}
              <text
                x={node.x + node.w / 2}
                y={node.y + node.h / 2 + 14}
                textAnchor="middle"
                dominantBaseline="middle"
                fill="#9ca3af"
                className="pointer-events-none"
                style={{ fontSize: 10 }}
                opacity={isDimmed ? 0.3 : 0.6}
              >
                {node.status.category?.replace('_', ' ') || ''}
              </text>
              {/* Initial badge */}
              {isInitial && (
                <g>
                  <rect
                    x={node.x + node.w / 2 - 28}
                    y={node.y - 10}
                    width={56} height={16}
                    rx={8} ry={8}
                    fill="#6366f1"
                  />
                  <text
                    x={node.x + node.w / 2}
                    y={node.y - 1}
                    textAnchor="middle"
                    dominantBaseline="middle"
                    fill="white"
                    style={{ fontSize: 9, fontWeight: 700, letterSpacing: '0.05em' }}
                  >
                    INITIAL
                  </text>
                </g>
              )}
              {/* Color dot from status.color */}
              {node.status.color && (
                <circle
                  cx={node.x + 16} cy={node.y + node.h / 2}
                  r={5}
                  fill={node.status.color}
                  opacity={isDimmed ? 0.3 : 1}
                />
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
