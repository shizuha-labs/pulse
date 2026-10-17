import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useEditor, EditorContent } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Image from '@tiptap/extension-image'
import Link from '@tiptap/extension-link'
import Mention from '@tiptap/extension-mention'
import { Table } from '@tiptap/extension-table'
import { TableRow } from '@tiptap/extension-table-row'
import { TableHeader } from '@tiptap/extension-table-header'
import { TableCell } from '@tiptap/extension-table-cell'
import {
  Bold,
  Italic,
  List,
  ListOrdered,
  Code,
  Link as LinkIcon,
  Image as ImageIcon,
  Send,
  Loader2,
  AtSign,
  Table as TableIcon,
} from 'lucide-react'
import { cn } from '../utils/cn'
import { attachmentsApi } from '../services/api'
import { createMentionSuggestion } from '../hooks/useMentionSuggestion'

const MenuButton = ({ onClick, isActive, disabled, children, title }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    title={title}
    className={cn(
      'p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors disabled:opacity-50',
      isActive && 'bg-gray-200 dark:bg-gray-600 text-brand-600 dark:text-brand-400'
    )}
  >
    {children}
  </button>
)

export default function CommentEditor({
  itemId,
  placeholder = 'Write a comment...',
  initialContent = '',
  onSubmit,
  onCancel,
  submitLabel = 'Send',
  isSubmitting = false,
  autoFocus = false,
  minHeight = '80px',
  mentionableUsers = [], // Array of { id, label, email }
}) {
  const editorRef = useRef(null)
  const [isUploading, setIsUploading] = useState(false)
  const [hasContent, setHasContent] = useState(false)

  const uploadImage = useCallback(async (file) => {
    if (!itemId) {
      console.error('No itemId provided for image upload')
      return null
    }

    if (!file || file.size === 0) {
      console.error('Invalid file for upload')
      return null
    }

    setIsUploading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('item', itemId)
      formData.append('source', 'comment')

      const res = await attachmentsApi.upload(formData)
      // Only the server-decoded/re-encoded raster derivative is safe to embed
      // inline. Original uploads are intentionally download-only.
      return res.data.thumbnail_url || null
    } catch (err) {
      console.error('Failed to upload image:', err)
      alert('Failed to upload image. Please try again.')
      return null
    } finally {
      setIsUploading(false)
    }
  }, [itemId])

  // Create mention suggestion config
  const mentionSuggestion = useMemo(() => {
    if (!mentionableUsers || mentionableUsers.length === 0) {
      return null
    }
    return createMentionSuggestion(() => mentionableUsers)
  }, [mentionableUsers])

  // Build extensions array
  const extensions = useMemo(() => {
    const exts = [
      StarterKit.configure({
        heading: false,
        horizontalRule: false,
        blockquote: false,
        // TipTap 3 ships Link in StarterKit; we add our own below with
        // custom HTMLAttributes, so disable the bundled one to avoid
        // "Duplicate extension names found: ['link']".
        link: false,
      }),
      Placeholder.configure({
        placeholder,
      }),
      Image.configure({
        inline: true,
        allowBase64: true,
        HTMLAttributes: {
          class: 'max-w-full rounded-lg my-2',
        },
      }),
      Link.configure({
        openOnClick: false,
        HTMLAttributes: {
          class: 'text-brand-600 dark:text-brand-400 underline',
        },
      }),
      Table.configure({
        resizable: false,
        HTMLAttributes: {
          class: 'border-collapse w-full my-2',
        },
      }),
      TableRow,
      TableHeader,
      TableCell,
    ]

    // Add mention extension if users are available
    if (mentionSuggestion) {
      exts.push(
        Mention.configure({
          HTMLAttributes: {
            class: 'mention bg-brand-100 dark:bg-brand-900/50 text-brand-700 dark:text-brand-300 rounded px-1 py-0.5 font-medium',
          },
          suggestion: mentionSuggestion,
        })
      )
    }

    return exts
  }, [placeholder, mentionSuggestion])

  const editor = useEditor({
    extensions,
    content: initialContent,
    autofocus: autoFocus,
    editorProps: {
      attributes: {
        class: 'outline-none',
      },
      // Prevent TipTap from handling image paste - we handle it ourselves
      handlePaste: (view, event) => {
        const clipboardData = event.clipboardData
        if (!clipboardData) return false

        // Check if clipboard has image files
        const items = clipboardData.items
        if (items) {
          for (const item of items) {
            if (item.type.startsWith('image/')) {
              // Return true to prevent default TipTap paste handling
              // Our useEffect paste handler will process this
              return true
            }
          }
        }

        // Check files array
        const files = clipboardData.files
        if (files && files.length > 0) {
          for (const file of files) {
            if (file.type.startsWith('image/')) {
              return true
            }
          }
        }

        // Let TipTap handle non-image pastes normally
        return false
      },
      // Prevent TipTap from handling image drop - we handle it ourselves
      handleDrop: (view, event) => {
        const files = event.dataTransfer?.files
        if (!files || files.length === 0) return false

        for (const file of files) {
          if (file.type.startsWith('image/')) {
            // Return true to prevent default TipTap drop handling
            return true
          }
        }

        return false
      },
    },
    onUpdate: ({ editor }) => {
      // Check if editor has text content or images
      const text = editor.getText().trim()
      const html = editor.getHTML()
      const hasImages = html.includes('<img')
      setHasContent(!!text || hasImages)
    },
  })

  // Keep ref in sync with editor
  useEffect(() => {
    editorRef.current = editor
  }, [editor])

  // Initialize hasContent state when editor loads
  useEffect(() => {
    if (editor) {
      const text = editor.getText().trim()
      const html = editor.getHTML()
      const hasImages = html.includes('<img')
      setHasContent(!!text || hasImages)
    }
  }, [editor])

  // Handle paste events for images
  useEffect(() => {
    if (!editor) return

    const handlePaste = async (event) => {
      const clipboardData = event.clipboardData
      if (!clipboardData) return

      // Check for image files in clipboard items
      const items = clipboardData.items
      if (items) {
        for (const item of items) {
          if (item.type.startsWith('image/')) {
            event.preventDefault()
            event.stopPropagation()
            const file = item.getAsFile()
            if (file && file.size > 0) {
              const url = await uploadImage(file)
              if (url && editorRef.current) {
                editorRef.current.chain().focus().setImage({ src: url }).run()
              }
            } else {
              console.warn('Clipboard image file is empty or inaccessible')
            }
            return
          }
        }
      }

      // Check files array as fallback
      const files = clipboardData.files
      if (files && files.length > 0) {
        for (const file of files) {
          if (file.type.startsWith('image/')) {
            event.preventDefault()
            event.stopPropagation()
            if (file.size > 0) {
              const url = await uploadImage(file)
              if (url && editorRef.current) {
                editorRef.current.chain().focus().setImage({ src: url }).run()
              }
            }
            return
          }
        }
      }
    }

    const editorElement = editor.view.dom
    editorElement.addEventListener('paste', handlePaste)

    return () => {
      editorElement.removeEventListener('paste', handlePaste)
    }
  }, [editor, uploadImage])

  // Handle drop events for images
  useEffect(() => {
    if (!editor) return

    const handleDrop = async (event) => {
      const files = event.dataTransfer?.files
      if (!files || files.length === 0) return

      for (const file of files) {
        if (file.type.startsWith('image/')) {
          event.preventDefault()
          event.stopPropagation()
          const url = await uploadImage(file)
          if (url && editorRef.current) {
            editorRef.current.chain().focus().setImage({ src: url }).run()
          }
          return
        }
      }
    }

    const editorElement = editor.view.dom
    editorElement.addEventListener('drop', handleDrop)

    return () => {
      editorElement.removeEventListener('drop', handleDrop)
    }
  }, [editor, uploadImage])

  // Update editor content when initialContent changes
  useEffect(() => {
    if (editor && initialContent !== editor.getHTML()) {
      editor.commands.setContent(initialContent)
    }
  }, [initialContent, editor])

  const handleSubmit = () => {
    if (!editor || isSubmitting || isUploading) return

    const html = editor.getHTML()
    const text = editor.getText()

    // Don't submit empty comments (no text and no images)
    const hasImages = html.includes('<img')
    if (!text.trim() && !hasImages) return

    // Extract mentioned user IDs from the JSON content
    const json = editor.getJSON()
    const mentionedUserIds = []
    const extractMentions = (node) => {
      if (node.type === 'mention' && node.attrs?.id) {
        mentionedUserIds.push(node.attrs.id)
      }
      if (node.content) {
        node.content.forEach(extractMentions)
      }
    }
    extractMentions(json)

    onSubmit?.({
      html,
      text,
      json,
      mentionedUserIds,
    })

    // Clear editor after submit
    editor.commands.clearContent()
    setHasContent(false)
  }

  const addLink = () => {
    const url = window.prompt('Enter URL:')
    if (url && editor) {
      editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run()
    }
  }

  const addImage = async () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*'
    input.onchange = async (e) => {
      const file = e.target.files?.[0]
      if (file) {
        const url = await uploadImage(file)
        if (url && editor) {
          editor.chain().focus().setImage({ src: url }).run()
        }
      }
    }
    input.click()
  }

  const insertMention = () => {
    if (editor && mentionableUsers.length > 0) {
      // Insert @ character to trigger mention dropdown
      editor.chain().focus().insertContent('@').run()
    }
  }

  const isDisabled = !hasContent || isSubmitting || isUploading
  const hasMentions = mentionableUsers && mentionableUsers.length > 0

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden bg-white dark:bg-gray-900">
      {/* Upload indicator */}
      {isUploading && (
        <div className="px-3 py-2 bg-blue-50 dark:bg-blue-900/20 border-b border-blue-200 dark:border-blue-800 flex items-center gap-2 text-sm text-blue-600 dark:text-blue-400">
          <Loader2 className="animate-spin h-4 w-4" />
          Uploading image...
        </div>
      )}

      {/* Editor */}
      <EditorContent
        editor={editor}
        className={cn(
          'prose prose-sm dark:prose-invert max-w-none px-3 py-2',
          '[&_.ProseMirror]:outline-none [&_.ProseMirror]:min-h-[60px]',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:content-[attr(data-placeholder)]',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:text-gray-400',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:dark:text-gray-500',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:float-left',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:pointer-events-none',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:h-0',
          '[&_.ProseMirror_img]:max-h-[200px]',
          '[&_.ProseMirror_img]:object-contain',
        )}
        style={{ minHeight }}
      />

      {/* Toolbar */}
      <div className="flex items-center justify-between px-2 py-1.5 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
        <div className="flex items-center gap-0.5">
          <MenuButton
            onClick={() => editor?.chain().focus().toggleBold().run()}
            isActive={editor?.isActive('bold')}
            disabled={isUploading}
            title="Bold (Ctrl+B)"
          >
            <Bold className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={() => editor?.chain().focus().toggleItalic().run()}
            isActive={editor?.isActive('italic')}
            disabled={isUploading}
            title="Italic (Ctrl+I)"
          >
            <Italic className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={() => editor?.chain().focus().toggleCode().run()}
            isActive={editor?.isActive('code')}
            disabled={isUploading}
            title="Code"
          >
            <Code className="h-4 w-4" />
          </MenuButton>

          <div className="w-px h-4 bg-gray-300 dark:bg-gray-600 mx-1" />

          <MenuButton
            onClick={() => editor?.chain().focus().toggleBulletList().run()}
            isActive={editor?.isActive('bulletList')}
            disabled={isUploading}
            title="Bullet List"
          >
            <List className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={() => editor?.chain().focus().toggleOrderedList().run()}
            isActive={editor?.isActive('orderedList')}
            disabled={isUploading}
            title="Numbered List"
          >
            <ListOrdered className="h-4 w-4" />
          </MenuButton>

          <div className="w-px h-4 bg-gray-300 dark:bg-gray-600 mx-1" />

          <MenuButton
            onClick={addLink}
            isActive={editor?.isActive('link')}
            disabled={isUploading}
            title="Add Link"
          >
            <LinkIcon className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={addImage}
            disabled={isUploading}
            title="Add Image (or paste/drop an image)"
          >
            <ImageIcon className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={() => editor?.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()}
            disabled={isUploading || editor?.isActive('table')}
            title="Insert Table"
          >
            <TableIcon className="h-4 w-4" />
          </MenuButton>
          {hasMentions && (
            <MenuButton
              onClick={insertMention}
              disabled={isUploading}
              title="Mention someone (@)"
            >
              <AtSign className="h-4 w-4" />
            </MenuButton>
          )}
        </div>

        <div className="flex items-center gap-2">
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              disabled={isUploading}
              className="text-sm text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 disabled:opacity-50"
            >
              Cancel
            </button>
          )}
          <button
            type="button"
            onClick={handleSubmit}
            disabled={isDisabled}
            className={cn(
              'btn-primary btn-sm flex items-center gap-1.5',
              isDisabled && 'opacity-50 cursor-not-allowed'
            )}
          >
            {isSubmitting ? (
              <Loader2 className="animate-spin h-4 w-4" />
            ) : (
              <Send className="h-4 w-4" />
            )}
            {submitLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
