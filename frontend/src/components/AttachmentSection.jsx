import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Paperclip,
  Upload,
  Download,
  Trash2,
  File,
  Image,
  FileText,
  FileSpreadsheet,
  FileCode,
  Loader2,
  X,
  ZoomIn,
} from 'lucide-react'
import { attachmentsApi } from '../services/api'
import { cn } from '../utils/cn'
import { formatDistanceToNow } from 'date-fns'

// File type to icon mapping
const FILE_ICONS = {
  'image': Image,
  'application/pdf': FileText,
  'text': FileText,
  'application/vnd.ms-excel': FileSpreadsheet,
  'application/vnd.openxmlformats-officedocument.spreadsheetml': FileSpreadsheet,
  'application/json': FileCode,
  'application/javascript': FileCode,
  'text/html': FileCode,
  'text/css': FileCode,
}

function getFileIcon(mimeType) {
  for (const [key, Icon] of Object.entries(FILE_ICONS)) {
    if (mimeType.startsWith(key) || mimeType.includes(key)) {
      return Icon
    }
  }
  return File
}

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export default function AttachmentSection({ itemId }) {
  const queryClient = useQueryClient()
  const fileInputRef = useRef(null)
  const [isDragging, setIsDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [lightboxImage, setLightboxImage] = useState(null)

  // Fetch attachments
  const { data: attachments = [], isLoading } = useQuery({
    queryKey: ['attachments', itemId],
    queryFn: async () => {
      const res = await attachmentsApi.list(itemId)
      // API returns {results: [...]} (paginated) or {attachments: [...]}
      return res.data.results || res.data.attachments || res.data || []
    },
    enabled: !!itemId,
  })

  // Upload mutation
  const uploadMutation = useMutation({
    mutationFn: async (file) => {
      setUploading(true)
      const formData = new FormData()
      formData.append('file', file)
      formData.append('item', itemId)
      return attachmentsApi.upload(formData)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['attachments', itemId] })
      queryClient.invalidateQueries({ queryKey: ['task-activity', itemId] })
      setUploading(false)
    },
    onError: () => {
      setUploading(false)
    },
  })

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (attachmentId) => attachmentsApi.delete(attachmentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['attachments', itemId] })
      queryClient.invalidateQueries({ queryKey: ['task-activity', itemId] })
    },
  })

  const handleFileSelect = (e) => {
    const files = e.target.files
    if (files?.length) {
      Array.from(files).forEach(file => uploadMutation.mutate(file))
    }
  }

  const handleDrop = (e) => {
    e.preventDefault()
    setIsDragging(false)
    const files = e.dataTransfer.files
    if (files?.length) {
      Array.from(files).forEach(file => uploadMutation.mutate(file))
    }
  }

  const handleDragOver = (e) => {
    e.preventDefault()
    setIsDragging(true)
  }

  const handleDragLeave = () => {
    setIsDragging(false)
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="animate-spin text-gray-400" size={24} />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Upload Area */}
      <div
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        className={cn(
          'border-2 border-dashed rounded-lg p-6 text-center transition-colors',
          isDragging
            ? 'border-brand-500 bg-brand-50 dark:bg-brand-900/20'
            : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
        )}
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
          onChange={handleFileSelect}
          className="hidden"
        />
        <div className="flex flex-col items-center gap-2">
          {uploading ? (
            <>
              <Loader2 className="animate-spin text-brand-500" size={32} />
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Uploading...
              </p>
            </>
          ) : (
            <>
              <Upload size={32} className="text-gray-400" />
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Drag and drop files here, or{' '}
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="text-brand-600 dark:text-brand-400 hover:underline"
                >
                  browse
                </button>
              </p>
            </>
          )}
        </div>
      </div>

      {/* Attachments List */}
      {attachments.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
          No attachments yet
        </p>
      ) : (
        <div className="space-y-2">
          {attachments.map((attachment) => {
            const FileIcon = getFileIcon(attachment.file_type)
            const isImage = attachment.is_image

            return (
              <div
                key={attachment.id}
                className="flex items-center gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-800 group"
              >
                {/* Thumbnail or Icon */}
                {isImage && attachment.thumbnail_url ? (
                  <button
                    onClick={() => setLightboxImage(attachment)}
                    className="relative w-12 h-12 rounded overflow-hidden flex-shrink-0 hover:opacity-80 transition-opacity"
                  >
                    <img
                      src={attachment.thumbnail_url}
                      alt={attachment.filename}
                      className="w-full h-full object-cover"
                    />
                    <div className="absolute inset-0 flex items-center justify-center bg-black/0 hover:bg-black/30 transition-colors">
                      <ZoomIn size={16} className="text-white opacity-0 group-hover:opacity-100" />
                    </div>
                  </button>
                ) : (
                  <div className="w-12 h-12 rounded bg-gray-200 dark:bg-gray-700 flex items-center justify-center flex-shrink-0">
                    <FileIcon size={20} className="text-gray-500 dark:text-gray-400" />
                  </div>
                )}

                {/* File Info */}
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-gray-900 dark:text-white text-sm truncate">
                    {attachment.filename}
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {formatFileSize(attachment.file_size)} &bull;{' '}
                    {formatDistanceToNow(new Date(attachment.created_at), { addSuffix: true })}
                  </p>
                </div>

                {/* Actions */}
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <a
                    href={attachment.download_url}
                    download={attachment.filename}
                    className="btn-icon text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                    title="Download"
                  >
                    <Download size={16} />
                  </a>
                  <button
                    onClick={() => {
                      if (confirm('Delete this attachment?')) {
                        deleteMutation.mutate(attachment.id)
                      }
                    }}
                    disabled={deleteMutation.isPending}
                    className="btn-icon text-gray-400 hover:text-red-500"
                    title="Delete"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Lightbox */}
      {lightboxImage && (
        <div
          className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-4"
          onClick={() => setLightboxImage(null)}
        >
          <button
            onClick={() => setLightboxImage(null)}
            className="absolute top-4 right-4 text-white hover:text-gray-300"
          >
            <X size={32} />
          </button>
          <img
            src={lightboxImage.thumbnail_url}
            alt={lightboxImage.filename}
            className="max-w-full max-h-full object-contain"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  )
}
