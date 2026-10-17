import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useEditor, EditorContent } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Image from '@tiptap/extension-image'
import Link from '@tiptap/extension-link'
import Mention from '@tiptap/extension-mention'
import {
  Bold,
  Italic,
  List,
  ListOrdered,
  Code,
  Link as LinkIcon,
  Image as ImageIcon,
  Check,
  X,
  Loader2,
  Pencil,
  AtSign,
  ZoomIn,
} from 'lucide-react'
import { cn } from '../utils/cn'
import { attachmentsApi } from '../services/api'
import { createMentionSuggestion } from '../hooks/useMentionSuggestion'
import { ensureHtml } from '../utils/markdown'

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

export default function DescriptionEditor({
  itemId,
  content = '',
  onSave,
  isSaving = false,
  placeholder = 'Add a description...',
  mentionableUsers = [],
}) {
  const editorRef = useRef(null)
  const [isEditing, setIsEditing] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [originalContent, setOriginalContent] = useState(content)
  const [lightboxImage, setLightboxImage] = useState(null)

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
        heading: {
          levels: [1, 2, 3],
        },
        horizontalRule: false,
        blockquote: true,
        // TipTap 3 ships Link in StarterKit; we add our own below with
        // custom HTMLAttributes, so disable the bundled one to avoid
        // "Duplicate extension names found: ['link']".
        link: false,
      }),
      Placeholder.configure({
        placeholder: mentionableUsers.length > 0
          ? `${placeholder} (use @ to mention)`
          : placeholder,
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
  }, [placeholder, mentionSuggestion, mentionableUsers.length])

  const editor = useEditor({
    extensions,
    content: ensureHtml(content),
    editable: isEditing,
    editorProps: {
      attributes: {
        class: 'outline-none',
      },
      handlePaste: (view, event) => {
        if (!isEditing) return false
        const clipboardData = event.clipboardData
        if (!clipboardData) return false

        const items = clipboardData.items
        if (items) {
          for (const item of items) {
            if (item.type.startsWith('image/')) {
              return true
            }
          }
        }

        const files = clipboardData.files
        if (files && files.length > 0) {
          for (const file of files) {
            if (file.type.startsWith('image/')) {
              return true
            }
          }
        }

        return false
      },
      handleDrop: (view, event) => {
        if (!isEditing) return false
        const files = event.dataTransfer?.files
        if (!files || files.length === 0) return false

        for (const file of files) {
          if (file.type.startsWith('image/')) {
            return true
          }
        }

        return false
      },
    },
  })

  useEffect(() => {
    editorRef.current = editor
  }, [editor])

  // Update editable state when isEditing changes
  useEffect(() => {
    if (editor) {
      editor.setEditable(isEditing)
      if (isEditing) {
        setTimeout(() => editor.commands.focus('end'), 0)
      }
    }
  }, [isEditing, editor])

  // Update content when prop changes — convert markdown to HTML if needed
  useEffect(() => {
    if (editor && !isEditing) {
      const htmlContent = ensureHtml(content)
      if (htmlContent !== editor.getHTML()) {
        editor.commands.setContent(htmlContent)
        setOriginalContent(htmlContent)
      }
    }
  }, [content, editor, isEditing])

  // Handle paste events for images
  useEffect(() => {
    if (!editor || !isEditing) return

    const handlePaste = async (event) => {
      const clipboardData = event.clipboardData
      if (!clipboardData) return

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
            }
            return
          }
        }
      }

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
  }, [editor, isEditing, uploadImage])

  // Handle drop events for images
  useEffect(() => {
    if (!editor || !isEditing) return

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
  }, [editor, isEditing, uploadImage])

  const handleSave = () => {
    if (!editor || isSaving || isUploading) return

    const html = editor.getHTML()
    // Check for empty content (just <p></p> or whitespace)
    const isEmpty = !editor.getText().trim() && !html.includes('<img')

    onSave?.(isEmpty ? '' : html)
    setOriginalContent(isEmpty ? '' : html)
    setIsEditing(false)
  }

  const handleCancel = () => {
    if (editor) {
      editor.commands.setContent(originalContent)
    }
    setIsEditing(false)
  }

  const handleEditClick = () => {
    setOriginalContent(editor?.getHTML() || content)
    setIsEditing(true)
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
      editor.chain().focus().insertContent('@').run()
    }
  }

  const isDisabled = isSaving || isUploading
  const hasMentions = mentionableUsers && mentionableUsers.length > 0

  // Render view mode
  if (!isEditing) {
    const displayContent = editor?.getHTML() || content
    const isEmpty = !displayContent || displayContent === '<p></p>' || !editor?.getText().trim() && !displayContent.includes('<img')

    // Handle click - show lightbox for images, edit mode for text
    const handleContentClick = (e) => {
      // Check if clicked on an image
      if (e.target.tagName === 'IMG') {
        e.stopPropagation()
        setLightboxImage(e.target.src)
        return
      }
      handleEditClick()
    }

    return (
      <div className="group relative">
        {isEmpty ? (
          <button
            onClick={handleEditClick}
            className="w-full text-left text-gray-400 dark:text-gray-500 italic hover:text-gray-600 dark:hover:text-gray-400 transition-colors py-2"
          >
            Click to add a description...
          </button>
        ) : (
          <>
            <div
              className="prose prose-sm dark:prose-invert max-w-none text-gray-600 dark:text-gray-400 [&_img]:max-h-[300px] [&_img]:object-contain [&_img]:cursor-zoom-in [&_img]:hover:opacity-80 [&_img]:transition-opacity [&_.mention]:bg-brand-100 [&_.mention]:dark:bg-brand-900/50 [&_.mention]:text-brand-700 [&_.mention]:dark:text-brand-300 [&_.mention]:rounded [&_.mention]:px-1 [&_.mention]:py-0.5 [&_.mention]:font-medium cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-lg p-2 -m-2 transition-colors"
              onClick={handleContentClick}
              dangerouslySetInnerHTML={{ __html: displayContent }}
            />
            <button
              onClick={handleEditClick}
              className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity btn-icon"
              title="Edit description"
            >
              <Pencil size={14} />
            </button>
          </>
        )}

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
      </div>
    )
  }

  // Render edit mode
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden bg-white dark:bg-gray-900">
      {isUploading && (
        <div className="px-3 py-2 bg-blue-50 dark:bg-blue-900/20 border-b border-blue-200 dark:border-blue-800 flex items-center gap-2 text-sm text-blue-600 dark:text-blue-400">
          <Loader2 className="animate-spin h-4 w-4" />
          Uploading image...
        </div>
      )}

      <EditorContent
        editor={editor}
        className={cn(
          'prose prose-sm dark:prose-invert max-w-none px-3 py-2',
          '[&_.ProseMirror]:outline-none [&_.ProseMirror]:min-h-[100px]',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:content-[attr(data-placeholder)]',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:text-gray-400',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:dark:text-gray-500',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:float-left',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:pointer-events-none',
          '[&_.ProseMirror_p.is-editor-empty:first-child]:before:h-0',
          '[&_.ProseMirror_img]:max-h-[200px]',
          '[&_.ProseMirror_img]:object-contain',
        )}
      />

      {/* Toolbar */}
      <div className="flex items-center justify-between px-2 py-1.5 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
        <div className="flex items-center gap-0.5">
          <MenuButton
            onClick={() => editor?.chain().focus().toggleBold().run()}
            isActive={editor?.isActive('bold')}
            disabled={isDisabled}
            title="Bold (Ctrl+B)"
          >
            <Bold className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={() => editor?.chain().focus().toggleItalic().run()}
            isActive={editor?.isActive('italic')}
            disabled={isDisabled}
            title="Italic (Ctrl+I)"
          >
            <Italic className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={() => editor?.chain().focus().toggleCode().run()}
            isActive={editor?.isActive('code')}
            disabled={isDisabled}
            title="Code"
          >
            <Code className="h-4 w-4" />
          </MenuButton>

          <div className="w-px h-4 bg-gray-300 dark:bg-gray-600 mx-1" />

          <MenuButton
            onClick={() => editor?.chain().focus().toggleBulletList().run()}
            isActive={editor?.isActive('bulletList')}
            disabled={isDisabled}
            title="Bullet List"
          >
            <List className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={() => editor?.chain().focus().toggleOrderedList().run()}
            isActive={editor?.isActive('orderedList')}
            disabled={isDisabled}
            title="Numbered List"
          >
            <ListOrdered className="h-4 w-4" />
          </MenuButton>

          <div className="w-px h-4 bg-gray-300 dark:bg-gray-600 mx-1" />

          <MenuButton
            onClick={addLink}
            isActive={editor?.isActive('link')}
            disabled={isDisabled}
            title="Add Link"
          >
            <LinkIcon className="h-4 w-4" />
          </MenuButton>
          <MenuButton
            onClick={addImage}
            disabled={isDisabled}
            title="Add Image (or paste/drop)"
          >
            <ImageIcon className="h-4 w-4" />
          </MenuButton>
          {hasMentions && (
            <MenuButton
              onClick={insertMention}
              disabled={isDisabled}
              title="Mention someone (@)"
            >
              <AtSign className="h-4 w-4" />
            </MenuButton>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleCancel}
            disabled={isDisabled}
            className="btn-icon text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            title="Cancel"
          >
            <X className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={isDisabled}
            className="btn-icon text-green-600 hover:text-green-700 dark:text-green-500 dark:hover:text-green-400"
            title="Save"
          >
            {isSaving ? (
              <Loader2 className="animate-spin h-4 w-4" />
            ) : (
              <Check className="h-4 w-4" />
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
