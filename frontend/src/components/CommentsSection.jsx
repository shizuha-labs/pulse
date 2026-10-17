import { useState, useEffect, useMemo } from 'react'
import { ensureHtml } from '../utils/markdown'
import {
  MessageSquare,
  Edit2,
  Trash2,
  MoreVertical,
  Loader2,
  Smile,
  Reply,
  ChevronDown,
  ChevronRight,
  X,
} from 'lucide-react'
import { commentsApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { cn } from '../utils/cn'
import { formatDistanceToNow, format } from 'date-fns'
import CommentEditor from './CommentEditor'

// Available reactions with their emoji and display
const REACTIONS = [
  { key: 'thumbs_up', emoji: '👍', label: '+1' },
  { key: 'thumbs_down', emoji: '👎', label: '-1' },
  { key: 'heart', emoji: '❤️', label: 'heart' },
  { key: 'laugh', emoji: '😄', label: 'laugh' },
  { key: 'confused', emoji: '😕', label: 'confused' },
  { key: 'celebrate', emoji: '🎉', label: 'celebrate' },
]

// Single comment component (recursive for replies)
function CommentItem({
  comment,
  itemId,
  mentionableUsers,
  depth = 0,
  onReply,
  onEdit,
  onDelete,
  editingId,
  editContent,
  onEditSubmit,
  onCancelEdit,
  replyingToId,
  onReplySubmit,
  onCancelReply,
  isOwnComment,
  openMenuId,
  setOpenMenuId,
  openReactionPickerId,
  setOpenReactionPickerId,
  reactionLoading,
  onAddReaction,
  onRemoveReaction,
  hasUserReacted,
  getReactionCount,
  collapsedThreads,
  toggleThread,
  allComments,
  onImageClick,
}) {
  const replies = allComments.filter(c => c.parent === comment.id)
  const hasReplies = replies.length > 0
  const isCollapsed = collapsedThreads.has(comment.id)
  const maxDepth = 3 // Maximum nesting depth before flattening

  return (
    <div className={cn('relative', depth > 0 && 'ml-6 pl-4 border-l-2 border-gray-200 dark:border-gray-700')}>
      <div className="p-3 rounded-lg bg-gray-50 dark:bg-gray-800">
        {editingId === comment.id ? (
          // Edit mode with rich text editor
          <CommentEditor
            itemId={itemId}
            initialContent={editContent}
            onSubmit={(content) => onEditSubmit(comment.id, content)}
            onCancel={onCancelEdit}
            submitLabel="Save"
            autoFocus
            minHeight="60px"
            mentionableUsers={mentionableUsers}
          />
        ) : (
          // View mode
          <>
            <div className="flex items-start justify-between mb-2">
              <div className="flex items-center gap-2">
                <div className="w-7 h-7 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center flex-shrink-0">
                  <span className="text-xs text-brand-600 dark:text-brand-400 font-medium">
                    {(comment.author_display_name || comment.author_email || '?')[0].toUpperCase()}
                  </span>
                </div>
                <div className="min-w-0">
                  <span className="font-medium text-sm text-gray-900 dark:text-white">
                    {comment.author_display_name || comment.author_email?.split('@')[0]}
                  </span>
                  <span className="text-xs text-gray-500 dark:text-gray-400 ml-2">
                    {formatDistanceToNow(new Date(comment.created_at), { addSuffix: true })}
                    {comment.is_edited && ' (edited)'}
                  </span>
                </div>
              </div>

              {/* Actions menu */}
              <div className="flex items-center gap-1">
                {/* Reply button */}
                {depth < maxDepth && (
                  <button
                    onClick={() => onReply(comment.id)}
                    className="btn-icon text-gray-400 hover:text-brand-600 dark:hover:text-brand-400"
                    title="Reply"
                  >
                    <Reply size={14} />
                  </button>
                )}

                {isOwnComment(comment) && (
                  <div className="relative" data-menu>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        setOpenMenuId(openMenuId === comment.id ? null : comment.id)
                      }}
                      className="btn-icon text-gray-400 hover:text-gray-600"
                    >
                      <MoreVertical size={14} />
                    </button>
                    {openMenuId === comment.id && (
                      <div className="dropdown-menu absolute right-0 top-full mt-1 min-w-[100px] z-40">
                        <button
                          className="dropdown-item"
                          onClick={() => onEdit(comment)}
                        >
                          <Edit2 size={14} /> Edit
                        </button>
                        <button
                          className="dropdown-item-danger"
                          onClick={() => {
                            setOpenMenuId(null)
                            onDelete(comment.id)
                          }}
                        >
                          <Trash2 size={14} /> Delete
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            <div
              className="prose prose-sm dark:prose-invert max-w-none text-gray-700 dark:text-gray-300 [&_img]:max-h-[200px] [&_img]:rounded-lg [&_img]:my-2 [&_img]:cursor-zoom-in [&_img]:hover:opacity-80 [&_img]:transition-opacity [&_.mention]:bg-brand-100 [&_.mention]:dark:bg-brand-900/50 [&_.mention]:text-brand-700 [&_.mention]:dark:text-brand-300 [&_.mention]:rounded [&_.mention]:px-1 [&_.mention]:py-0.5 [&_.mention]:font-medium"
              onClick={(e) => {
                if (e.target.tagName === 'IMG') {
                  e.stopPropagation()
                  onImageClick?.(e.target.src)
                }
              }}
              dangerouslySetInnerHTML={{ __html: ensureHtml(comment.content) }}
            />

            {/* Reactions and Reply count */}
            <div className="flex items-center gap-2 mt-2 flex-wrap">
              {/* Existing reactions */}
              {REACTIONS.map(({ key, emoji }) => {
                const count = getReactionCount(comment, key)
                if (count === 0) return null
                const userReacted = hasUserReacted(comment, key)
                const isLoading = reactionLoading === `${comment.id}-${key}`

                return (
                  <button
                    key={key}
                    onClick={() => userReacted
                      ? onRemoveReaction(comment.id, key)
                      : onAddReaction(comment.id, key)
                    }
                    disabled={isLoading}
                    className={cn(
                      'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs transition-colors',
                      userReacted
                        ? 'bg-brand-100 dark:bg-brand-900/50 text-brand-700 dark:text-brand-300 border border-brand-300 dark:border-brand-700'
                        : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
                    )}
                    title={userReacted ? 'Remove reaction' : 'Add reaction'}
                  >
                    {isLoading ? (
                      <Loader2 className="animate-spin" size={12} />
                    ) : (
                      <span>{emoji}</span>
                    )}
                    <span>{count}</span>
                  </button>
                )
              })}

              {/* Add reaction button */}
              <div className="relative" data-reaction-picker>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setOpenReactionPickerId(
                      openReactionPickerId === comment.id ? null : comment.id
                    )
                  }}
                  className="inline-flex items-center justify-center w-6 h-6 rounded-full text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                  title="Add reaction"
                >
                  <Smile size={14} />
                </button>

                {/* Reaction picker dropdown */}
                {openReactionPickerId === comment.id && (
                  <div className="absolute left-0 bottom-full mb-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 p-1 flex gap-1 z-40">
                    {REACTIONS.map(({ key, emoji, label }) => (
                      <button
                        key={key}
                        onClick={() => onAddReaction(comment.id, key)}
                        className="w-8 h-8 flex items-center justify-center rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                        title={label}
                      >
                        {emoji}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Reply count / collapse toggle */}
              {hasReplies && (
                <button
                  onClick={() => toggleThread(comment.id)}
                  className="inline-flex items-center gap-1 px-2 py-0.5 text-xs text-gray-500 dark:text-gray-400 hover:text-brand-600 dark:hover:text-brand-400 transition-colors"
                >
                  {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                  {replies.length} {replies.length === 1 ? 'reply' : 'replies'}
                </button>
              )}
            </div>
          </>
        )}
      </div>

      {/* Reply form */}
      {replyingToId === comment.id && (
        <div className="mt-2 ml-6 pl-4 border-l-2 border-brand-200 dark:border-brand-700">
          <CommentEditor
            itemId={itemId}
            placeholder={`Reply to ${comment.author_display_name || comment.author_email?.split('@')[0]}...`}
            onSubmit={(content) => onReplySubmit(comment.id, content)}
            onCancel={onCancelReply}
            submitLabel="Reply"
            autoFocus
            minHeight="60px"
            mentionableUsers={mentionableUsers}
          />
        </div>
      )}

      {/* Replies (nested comments) */}
      {hasReplies && !isCollapsed && (
        <div className="mt-2 space-y-2">
          {replies.map(reply => (
            <CommentItem
              key={reply.id}
              comment={reply}
              itemId={itemId}
              mentionableUsers={mentionableUsers}
              depth={depth + 1}
              onReply={onReply}
              onEdit={onEdit}
              onDelete={onDelete}
              editingId={editingId}
              editContent={editContent}
              onEditSubmit={onEditSubmit}
              onCancelEdit={onCancelEdit}
              replyingToId={replyingToId}
              onReplySubmit={onReplySubmit}
              onCancelReply={onCancelReply}
              isOwnComment={isOwnComment}
              openMenuId={openMenuId}
              setOpenMenuId={setOpenMenuId}
              openReactionPickerId={openReactionPickerId}
              setOpenReactionPickerId={setOpenReactionPickerId}
              reactionLoading={reactionLoading}
              onAddReaction={onAddReaction}
              onRemoveReaction={onRemoveReaction}
              hasUserReacted={hasUserReacted}
              getReactionCount={getReactionCount}
              collapsedThreads={collapsedThreads}
              toggleThread={toggleThread}
              allComments={allComments}
              onImageClick={onImageClick}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// PLS-1287: friendly label for a system/lifecycle event, preferring the
// typed origin_event over the coarse origin_kind class.
const systemEventLabel = (comment) => {
  const ev = (comment.origin_event || '').trim()
  if (ev) return ev
  const kindLabels = {
    WORKFLOW_LIFECYCLE: 'lifecycle',
    VALIDATOR_AUDIT: 'validator audit',
    RESOLVER_AUDIT: 'resolver audit',
    FOCUS_REFUSED: 'focus-guard',
  }
  return kindLabels[comment.origin_kind] || 'system'
}

// PLS-1287 trail hygiene: one expandable digest line for a run of consecutive
// system/lifecycle comments (failover, rebalance, capacity-queue, escalation
// storms). Substantive content never collapses; the full entries render on
// expand so no information is lost — the trail just stops drowning them.
function SystemEventDigest({ items, renderComment }) {
  const [expanded, setExpanded] = useState(false)

  const counts = useMemo(() => {
    const m = new Map()
    for (const c of items) {
      const label = systemEventLabel(c)
      m.set(label, (m.get(label) || 0) + 1)
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  }, [items])

  const first = items[0]?.created_at
  const last = items[items.length - 1]?.created_at
  const range = (first && last && first !== last)
    ? ` · ${format(new Date(first), 'MMM d')} → ${format(new Date(last), 'MMM d')}`
    : (first ? ` · ${format(new Date(first), 'MMM d')}` : '')

  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-50/60 dark:bg-gray-800/60 border border-dashed border-gray-200 dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400 hover:border-brand-300 dark:hover:border-brand-700 hover:text-gray-600 dark:hover:text-gray-300 transition-colors text-left"
        title={expanded ? 'Collapse system events' : 'Show system events'}
      >
        {expanded ? <ChevronDown size={12} className="flex-shrink-0" /> : <ChevronRight size={12} className="flex-shrink-0" />}
        <span className="truncate">
          ⟨{items.length} system {items.length === 1 ? 'event' : 'events'}{range} · {counts.map(([label, n]) => `${label} ×${n}`).join(', ')}⟩
        </span>
      </button>
      {expanded && (
        <div className="mt-2 space-y-2">
          {items.map((c) => renderComment(c))}
        </div>
      )}
    </div>
  )
}

export default function CommentsSection({ itemId, mentionableUsers = [] }) {
  const { user } = useAuth()
  const [comments, setComments] = useState([])
  const [loading, setLoading] = useState(true)
  const [posting, setPosting] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [editContent, setEditContent] = useState('')
  const [replyingToId, setReplyingToId] = useState(null)
  const [openMenuId, setOpenMenuId] = useState(null)
  const [openReactionPickerId, setOpenReactionPickerId] = useState(null)
  const [reactionLoading, setReactionLoading] = useState(null)
  const [collapsedThreads, setCollapsedThreads] = useState(new Set())
  const [lightboxImage, setLightboxImage] = useState(null)

  useEffect(() => {
    if (itemId) {
      loadComments()
    }
  }, [itemId])

  // Close reaction picker when clicking outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (openReactionPickerId && !e.target.closest('[data-reaction-picker]')) {
        setOpenReactionPickerId(null)
      }
      if (openMenuId && !e.target.closest('[data-menu]')) {
        setOpenMenuId(null)
      }
    }
    document.addEventListener('click', handleClickOutside)
    return () => document.removeEventListener('click', handleClickOutside)
  }, [openReactionPickerId, openMenuId])

  const loadComments = async () => {
    try {
      setLoading(true)
      const commentList = await commentsApi.listAll(itemId)
      setComments(Array.isArray(commentList) ? commentList : [])
    } catch (err) {
      console.error('Failed to load comments:', err)
      setComments([])
    } finally {
      setLoading(false)
    }
  }

  // Get top-level comments (no parent)
  const topLevelComments = useMemo(() => {
    return comments.filter(c => !c.parent)
  }, [comments])

  // Total comment count including replies
  const totalCount = comments.length

  // PLS-1287 trail hygiene: group consecutive top-level system/lifecycle
  // comments into digest runs; substantive comments always render inline.
  const trailGroups = useMemo(() => {
    const groups = []
    let run = null
    for (const c of topLevelComments) {
      if (c.is_system) {
        if (run) {
          run.items.push(c)
        } else {
          run = { kind: 'system', items: [c] }
          groups.push(run)
        }
      } else {
        run = null
        groups.push({ kind: 'comment', items: [c] })
      }
    }
    return groups
  }, [topLevelComments])

  const substantiveCount = comments.filter(c => !c.is_system).length
  const systemCount = totalCount - substantiveCount

  const handleSubmit = async ({ html, text }, parentId = null) => {
    // Allow submit if there's text OR if there are images
    const hasImages = html && html.includes('<img')
    if ((!text.trim() && !hasImages) || posting) return

    setPosting(true)
    try {
      await commentsApi.create({
        item: itemId,
        content: html,
        parent: parentId,
      })
      setReplyingToId(null)
      await loadComments()
    } catch (err) {
      console.error('Failed to post comment:', err)
    } finally {
      setPosting(false)
    }
  }

  const handleReplySubmit = async (parentId, { html, text }) => {
    await handleSubmit({ html, text }, parentId)
  }

  const handleEdit = async (commentId, { html, text }) => {
    // Allow edit if there's text OR if there are images
    const hasImages = html && html.includes('<img')
    if (!text.trim() && !hasImages) return

    try {
      await commentsApi.update(commentId, { content: html })
      setEditingId(null)
      setEditContent('')
      await loadComments()
    } catch (err) {
      console.error('Failed to update comment:', err)
    }
  }

  const handleDelete = async (commentId) => {
    if (!confirm('Delete this comment?')) return

    try {
      await commentsApi.delete(commentId)
      await loadComments()
    } catch (err) {
      console.error('Failed to delete comment:', err)
    }
  }

  const startEdit = (comment) => {
    setEditingId(comment.id)
    setEditContent(comment.content)
    setOpenMenuId(null)
  }

  const cancelEdit = () => {
    setEditingId(null)
    setEditContent('')
  }

  const startReply = (commentId) => {
    setReplyingToId(commentId)
  }

  const cancelReply = () => {
    setReplyingToId(null)
  }

  const isOwnComment = (comment) => {
    return user && (comment.author_id === user.id || comment.author_email === user.email)
  }

  const handleAddReaction = async (commentId, reaction) => {
    setReactionLoading(`${commentId}-${reaction}`)
    try {
      await commentsApi.addReaction(commentId, reaction)
      await loadComments()
    } catch (err) {
      console.error('Failed to add reaction:', err)
    } finally {
      setReactionLoading(null)
      setOpenReactionPickerId(null)
    }
  }

  const handleRemoveReaction = async (commentId, reaction) => {
    setReactionLoading(`${commentId}-${reaction}`)
    try {
      await commentsApi.removeReaction(commentId, reaction)
      await loadComments()
    } catch (err) {
      console.error('Failed to remove reaction:', err)
    } finally {
      setReactionLoading(null)
    }
  }

  const hasUserReacted = (comment, reactionKey) => {
    if (!user || !comment.reactions_summary) return false
    const reaction = comment.reactions_summary.find(r => r.reaction === reactionKey)
    return reaction?.user_reacted || false
  }

  const getReactionCount = (comment, reactionKey) => {
    if (!comment.reactions_summary) return 0
    const reaction = comment.reactions_summary.find(r => r.reaction === reactionKey)
    return reaction?.count || 0
  }

  const toggleThread = (commentId) => {
    setCollapsedThreads(prev => {
      const next = new Set(prev)
      if (next.has(commentId)) {
        next.delete(commentId)
      } else {
        next.add(commentId)
      }
      return next
    })
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="animate-spin text-gray-400" size={24} />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header — PLS-1287: the attention signal counts substantive content;
          system/lifecycle volume is reported separately, never inflating it. */}
      <div className="flex items-center gap-2 text-gray-700 dark:text-gray-300">
        <MessageSquare size={18} />
        <h3 className="font-medium">
          Comments {substantiveCount > 0 && `(${substantiveCount})`}
        </h3>
        {systemCount > 0 && (
          <span className="text-xs text-gray-400 dark:text-gray-500 font-normal">
            + {systemCount} system {systemCount === 1 ? 'event' : 'events'} (digested)
          </span>
        )}
      </div>

      {/* Comment List — PLS-1287: consecutive system comments render as one
          expandable digest line; substantive comments always render inline. */}
      <div className="space-y-3">
        {trailGroups.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
            No comments yet. Be the first to comment!
          </p>
        ) : (
          trailGroups.map((group, idx) =>
            group.kind === 'system' ? (
              <SystemEventDigest
                key={`digest-${group.items[0]?.id ?? idx}`}
                items={group.items}
                renderComment={(comment) => (
                  <CommentItem
                    comment={comment}
                    itemId={itemId}
                    mentionableUsers={mentionableUsers}
                    depth={0}
                    onReply={startReply}
                    onEdit={startEdit}
                    onDelete={handleDelete}
                    editingId={editingId}
                    editContent={editContent}
                    onEditSubmit={handleEdit}
                    onCancelEdit={cancelEdit}
                    replyingToId={replyingToId}
                    onReplySubmit={handleReplySubmit}
                    onCancelReply={cancelReply}
                    isOwnComment={isOwnComment}
                    openMenuId={openMenuId}
                    setOpenMenuId={setOpenMenuId}
                    openReactionPickerId={openReactionPickerId}
                    setOpenReactionPickerId={setOpenReactionPickerId}
                    reactionLoading={reactionLoading}
                    onAddReaction={handleAddReaction}
                    onRemoveReaction={handleRemoveReaction}
                    hasUserReacted={hasUserReacted}
                    getReactionCount={getReactionCount}
                    collapsedThreads={collapsedThreads}
                    toggleThread={toggleThread}
                    allComments={comments}
                    onImageClick={setLightboxImage}
                  />
                )}
              />
            ) : (
              <CommentItem
                key={group.items[0].id}
                comment={group.items[0]}
                itemId={itemId}
                mentionableUsers={mentionableUsers}
                depth={0}
                onReply={startReply}
                onEdit={startEdit}
                onDelete={handleDelete}
                editingId={editingId}
                editContent={editContent}
                onEditSubmit={handleEdit}
                onCancelEdit={cancelEdit}
                replyingToId={replyingToId}
                onReplySubmit={handleReplySubmit}
                onCancelReply={cancelReply}
                isOwnComment={isOwnComment}
                openMenuId={openMenuId}
                setOpenMenuId={setOpenMenuId}
                openReactionPickerId={openReactionPickerId}
                setOpenReactionPickerId={setOpenReactionPickerId}
                reactionLoading={reactionLoading}
                onAddReaction={handleAddReaction}
                onRemoveReaction={handleRemoveReaction}
                hasUserReacted={hasUserReacted}
                getReactionCount={getReactionCount}
                collapsedThreads={collapsedThreads}
                toggleThread={toggleThread}
                allComments={comments}
                onImageClick={setLightboxImage}
              />
            )
          )
        )}
      </div>

      {/* Image Lightbox */}
      {lightboxImage && (
        <div
          className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-4"
          onClick={() => setLightboxImage(null)}
        >
          <button
            onClick={() => setLightboxImage(null)}
            className="absolute top-4 right-4 text-white hover:text-gray-300 z-10"
          >
            <X size={32} />
          </button>
          <img
            src={lightboxImage}
            alt="Full size"
            className="max-w-full max-h-full object-contain"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}

      {/* New Comment Form */}
      <CommentEditor
        itemId={itemId}
        placeholder={mentionableUsers.length > 0 ? "Write a comment... (use @ to mention)" : "Write a comment... (paste images with Ctrl+V)"}
        onSubmit={handleSubmit}
        isSubmitting={posting}
        mentionableUsers={mentionableUsers}
      />
    </div>
  )
}
