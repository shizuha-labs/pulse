import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  Upload, FileJson, CheckCircle, XCircle, AlertTriangle, Loader2,
  ArrowRight, Download, Info, ChevronDown, ChevronRight, ExternalLink
} from 'lucide-react'
import { cn } from '../../utils/cn'
import api from '../../services/api'

const IMPORT_STEPS = [
  { id: 'upload', label: 'Upload Export' },
  { id: 'mapping', label: 'Map Fields' },
  { id: 'preview', label: 'Preview' },
  { id: 'import', label: 'Import' },
  { id: 'complete', label: 'Complete' },
]

const JIRA_ENTITY_TYPES = [
  { key: 'projects', label: 'Projects', icon: '📁' },
  { key: 'issue_types', label: 'Issue Types', icon: '🐛' },
  { key: 'issues', label: 'Issues', icon: '📋' },
  { key: 'workflows', label: 'Workflows', icon: '🔀' },
  { key: 'custom_fields', label: 'Custom Fields', icon: '📝' },
  { key: 'screens', label: 'Screens', icon: '🖥' },
  { key: 'permission_schemes', label: 'Permission Schemes', icon: '🔑' },
  { key: 'notification_schemes', label: 'Notification Schemes', icon: '🔔' },
  { key: 'boards', label: 'Boards', icon: '📊' },
  { key: 'sprints', label: 'Sprints', icon: '🏃' },
  { key: 'components', label: 'Components', icon: '🧩' },
  { key: 'versions', label: 'Versions', icon: '🏷' },
  { key: 'comments', label: 'Comments', icon: '💬' },
  { key: 'attachments', label: 'Attachments', icon: '📎' },
  { key: 'worklogs', label: 'Work Logs', icon: '⏱' },
]

export default function JiraImport() {
  const [currentStep, setCurrentStep] = useState('upload')
  const [importFile, setImportFile] = useState(null)
  const [importMethod, setImportMethod] = useState('file') // 'file' or 'cloud'
  const [cloudConfig, setCloudConfig] = useState({ url: '', email: '', api_token: '' })
  const [parsedData, setParsedData] = useState(null)
  const [fieldMappings, setFieldMappings] = useState({})
  const [selectedEntities, setSelectedEntities] = useState(
    Object.fromEntries(JIRA_ENTITY_TYPES.map(t => [t.key, true]))
  )
  const [importProgress, setImportProgress] = useState(null)
  const [importResult, setImportResult] = useState(null)
  const [expandedSections, setExpandedSections] = useState({})
  const [errors, setErrors] = useState([])

  const toggleSection = (key) => {
    setExpandedSections(prev => ({ ...prev, [key]: !prev[key] }))
  }

  // Parse uploaded JSON
  const parseMutation = useMutation({
    mutationFn: async (file) => {
      const text = await file.text()
      return JSON.parse(text)
    },
    onSuccess: (data) => {
      setParsedData(data)
      // Auto-detect available entities
      const detected = {}
      JIRA_ENTITY_TYPES.forEach(t => {
        detected[t.key] = !!(data[t.key] || data[t.key.replace('_', '')])
      })
      setSelectedEntities(prev => ({ ...prev, ...detected }))
      setCurrentStep('mapping')
    },
    onError: (err) => {
      setErrors([`Failed to parse file: ${err.message}`])
    },
  })

  // Cloud import - validate connection
  const validateCloudMutation = useMutation({
    mutationFn: async (config) => {
      const res = await api.post('/import/jira/validate/', config)
      return res.data
    },
    onSuccess: (data) => {
      setParsedData(data)
      setCurrentStep('mapping')
    },
    onError: (err) => {
      setErrors([err.response?.data?.detail || 'Failed to connect to Jira Cloud'])
    },
  })

  // Execute import
  const importMutation = useMutation({
    mutationFn: async (payload) => {
      const res = await api.post('/import/jira/execute/', payload)
      return res.data
    },
    onSuccess: (data) => {
      setImportResult(data)
      setCurrentStep('complete')
    },
    onError: (err) => {
      setErrors([err.response?.data?.detail || 'Import failed'])
      setImportResult({ status: 'failed', error: err.response?.data?.detail })
    },
  })

  const handleFileUpload = (e) => {
    const file = e.target.files[0]
    if (file) {
      setImportFile(file)
      setErrors([])
      parseMutation.mutate(file)
    }
  }

  const handleCloudConnect = () => {
    setErrors([])
    validateCloudMutation.mutate(cloudConfig)
  }

  const handleStartImport = () => {
    setCurrentStep('import')
    setImportProgress({ current: 0, total: Object.values(selectedEntities).filter(Boolean).length })

    const payload = {
      method: importMethod,
      data: parsedData,
      entities: selectedEntities,
      field_mappings: fieldMappings,
      ...(importMethod === 'cloud' ? { cloud_config: cloudConfig } : {}),
    }
    importMutation.mutate(payload)
  }

  const stepIndex = IMPORT_STEPS.findIndex(s => s.id === currentStep)

  return (
    <div className="space-y-6 max-w-4xl mx-auto">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Import from Jira</h1>
        <p className="text-gray-500 dark:text-gray-400">
          Import projects, issues, and configurations from Atlassian Jira
        </p>
      </div>

      {/* Progress Steps */}
      <div className="card p-4">
        <div className="flex items-center justify-between">
          {IMPORT_STEPS.map((step, idx) => (
            <div key={step.id} className="flex items-center flex-1">
              <div className="flex items-center gap-2">
                <div className={cn(
                  'w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium',
                  idx < stepIndex
                    ? 'bg-green-500 text-white'
                    : idx === stepIndex
                    ? 'bg-brand-600 text-white'
                    : 'bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-400'
                )}>
                  {idx < stepIndex ? <CheckCircle size={16} /> : idx + 1}
                </div>
                <span className={cn(
                  'text-sm font-medium hidden sm:inline',
                  idx <= stepIndex
                    ? 'text-gray-900 dark:text-gray-100'
                    : 'text-gray-400 dark:text-gray-500'
                )}>
                  {step.label}
                </span>
              </div>
              {idx < IMPORT_STEPS.length - 1 && (
                <div className={cn(
                  'flex-1 h-0.5 mx-3',
                  idx < stepIndex ? 'bg-green-500' : 'bg-gray-200 dark:bg-gray-700'
                )} />
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Errors */}
      {errors.length > 0 && (
        <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
          {errors.map((err, i) => (
            <div key={i} className="flex items-center gap-2 text-red-700 dark:text-red-300">
              <XCircle size={16} />
              <span className="text-sm">{err}</span>
            </div>
          ))}
        </div>
      )}

      {/* Step 1: Upload */}
      {currentStep === 'upload' && (
        <div className="space-y-6">
          {/* Import method selector */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <button
              onClick={() => setImportMethod('file')}
              className={cn(
                'card p-6 text-left border-2 transition-all',
                importMethod === 'file'
                  ? 'border-brand-500 bg-brand-50 dark:bg-brand-950'
                  : 'border-transparent hover:border-gray-300 dark:hover:border-gray-600'
              )}
            >
              <FileJson size={32} className="text-brand-600 dark:text-brand-400 mb-3" />
              <h3 className="font-semibold text-gray-900 dark:text-gray-100">JSON File Import</h3>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                Upload a Jira JSON export file from Jira Cloud or Server
              </p>
            </button>
            <button
              onClick={() => setImportMethod('cloud')}
              className={cn(
                'card p-6 text-left border-2 transition-all',
                importMethod === 'cloud'
                  ? 'border-brand-500 bg-brand-50 dark:bg-brand-950'
                  : 'border-transparent hover:border-gray-300 dark:hover:border-gray-600'
              )}
            >
              <Download size={32} className="text-brand-600 dark:text-brand-400 mb-3" />
              <h3 className="font-semibold text-gray-900 dark:text-gray-100">Jira Cloud Connect</h3>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                Connect directly to your Jira Cloud instance via API
              </p>
            </button>
          </div>

          {/* File upload */}
          {importMethod === 'file' && (
            <div className="card p-6">
              <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">Upload Jira Export</h3>
              <div className="border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-8 text-center hover:border-brand-400 transition-colors">
                <Upload size={48} className="mx-auto text-gray-400 mb-4" />
                <p className="text-gray-600 dark:text-gray-300 mb-2">
                  Drop your Jira export file here, or click to browse
                </p>
                <p className="text-sm text-gray-400 mb-4">Supports JSON exports from Jira Cloud and Server</p>
                <label className="btn-primary cursor-pointer inline-flex items-center gap-2">
                  <Upload size={16} />
                  Choose File
                  <input
                    type="file"
                    accept=".json,.zip"
                    onChange={handleFileUpload}
                    className="hidden"
                  />
                </label>
                {importFile && (
                  <p className="mt-3 text-sm text-green-600 dark:text-green-400">
                    Selected: {importFile.name} ({(importFile.size / 1024 / 1024).toFixed(2)} MB)
                  </p>
                )}
              </div>

              {parseMutation.isPending && (
                <div className="mt-4 flex items-center gap-2 text-brand-600 dark:text-brand-400">
                  <Loader2 size={16} className="animate-spin" />
                  <span>Parsing export file...</span>
                </div>
              )}

              {/* How to export from Jira */}
              <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-900/20 rounded-lg">
                <div className="flex items-start gap-2">
                  <Info size={16} className="text-blue-600 dark:text-blue-400 mt-0.5 flex-shrink-0" />
                  <div>
                    <p className="text-sm font-medium text-blue-800 dark:text-blue-200">
                      How to export from Jira
                    </p>
                    <ol className="text-sm text-blue-700 dark:text-blue-300 mt-1 space-y-1 list-decimal list-inside">
                      <li>Go to Jira Settings → System → Backup manager</li>
                      <li>Click "Create backup for cloud" or "Create backup for server"</li>
                      <li>Download the JSON export file</li>
                      <li>Upload the file here</li>
                    </ol>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Cloud connect */}
          {importMethod === 'cloud' && (
            <div className="card p-6">
              <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">Connect to Jira Cloud</h3>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Jira Instance URL
                  </label>
                  <input
                    type="url"
                    value={cloudConfig.url}
                    onChange={(e) => setCloudConfig({ ...cloudConfig, url: e.target.value })}
                    className="input w-full"
                    placeholder="https://your-org.atlassian.net"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Email Address
                  </label>
                  <input
                    type="email"
                    value={cloudConfig.email}
                    onChange={(e) => setCloudConfig({ ...cloudConfig, email: e.target.value })}
                    className="input w-full"
                    placeholder="admin@your-org.com"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    API Token
                  </label>
                  <input
                    type="password"
                    value={cloudConfig.api_token}
                    onChange={(e) => setCloudConfig({ ...cloudConfig, api_token: e.target.value })}
                    className="input w-full"
                    placeholder="Your Jira API token"
                  />
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    Generate at{' '}
                    <a
                      href="https://id.atlassian.com/manage-profile/security/api-tokens"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-brand-600 dark:text-brand-400 hover:underline inline-flex items-center gap-1"
                    >
                      Atlassian API Tokens <ExternalLink size={12} />
                    </a>
                  </p>
                </div>
                <button
                  onClick={handleCloudConnect}
                  disabled={!cloudConfig.url || !cloudConfig.email || !cloudConfig.api_token || validateCloudMutation.isPending}
                  className="btn-primary flex items-center gap-2"
                >
                  {validateCloudMutation.isPending ? (
                    <><Loader2 size={16} className="animate-spin" /> Connecting...</>
                  ) : (
                    <><ArrowRight size={16} /> Connect & Scan</>
                  )}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Step 2: Mapping */}
      {currentStep === 'mapping' && (
        <div className="space-y-6">
          {/* Entity selection */}
          <div className="card p-6">
            <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-2">Select What to Import</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              Choose which entities to import from your Jira data
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {JIRA_ENTITY_TYPES.map(entity => {
                const count = parsedData?.[entity.key]?.length || parsedData?.[entity.key.replace('_', '')]?.length || 0
                return (
                  <label
                    key={entity.key}
                    className={cn(
                      'flex items-center gap-3 p-3 rounded-lg border transition-colors cursor-pointer',
                      selectedEntities[entity.key]
                        ? 'border-brand-300 bg-brand-50 dark:bg-brand-950 dark:border-brand-700'
                        : 'border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800',
                      count === 0 && 'opacity-50 cursor-not-allowed'
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={selectedEntities[entity.key] && count > 0}
                      onChange={(e) => setSelectedEntities(prev => ({
                        ...prev, [entity.key]: e.target.checked
                      }))}
                      disabled={count === 0}
                      className="rounded border-gray-300 text-brand-600 focus:ring-brand-500"
                    />
                    <span className="text-lg">{entity.icon}</span>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                        {entity.label}
                      </p>
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        {count > 0 ? `${count} found` : 'Not available'}
                      </p>
                    </div>
                  </label>
                )
              })}
            </div>
          </div>

          {/* Field mapping preview */}
          <div className="card p-6">
            <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-2">Field Mapping</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              Standard Jira fields are automatically mapped. Custom field mappings can be configured below.
            </p>

            {/* Standard mappings */}
            <div className="space-y-2 mb-4">
              <h4 className="text-sm font-medium text-gray-700 dark:text-gray-300">Standard Field Mappings</h4>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
                {[
                  ['Summary', 'Title'],
                  ['Description', 'Description'],
                  ['Status', 'Status'],
                  ['Priority', 'Priority'],
                  ['Assignee', 'Assignee'],
                  ['Reporter', 'Reporter'],
                  ['Created', 'Created At'],
                  ['Updated', 'Updated At'],
                  ['Due Date', 'Due Date'],
                  ['Labels', 'Labels'],
                  ['Components', 'Components'],
                  ['Fix Version', 'Fix Version'],
                  ['Story Points', 'Story Points'],
                  ['Sprint', 'Sprint'],
                ].map(([jira, pulse]) => (
                  <div key={jira} className="flex items-center gap-2 py-1.5 px-3 bg-gray-50 dark:bg-gray-800 rounded">
                    <span className="text-gray-600 dark:text-gray-400 flex-1">{jira}</span>
                    <ArrowRight size={14} className="text-gray-400" />
                    <span className="text-gray-900 dark:text-gray-100 flex-1">{pulse}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Custom field mappings */}
            {parsedData?.custom_fields?.length > 0 && (
              <div>
                <button
                  onClick={() => toggleSection('customFieldMapping')}
                  className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300 mb-2"
                >
                  {expandedSections.customFieldMapping ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                  Custom Field Mappings ({parsedData.custom_fields.length})
                </button>
                {expandedSections.customFieldMapping && (
                  <div className="space-y-2">
                    {parsedData.custom_fields.map((cf, i) => (
                      <div key={i} className="flex items-center gap-2 py-2 px-3 bg-gray-50 dark:bg-gray-800 rounded">
                        <span className="text-sm text-gray-600 dark:text-gray-400 flex-1 truncate">
                          {cf.name || cf.key || `Custom Field ${i + 1}`}
                        </span>
                        <ArrowRight size={14} className="text-gray-400 flex-shrink-0" />
                        <select
                          value={fieldMappings[cf.key || cf.id] || 'auto'}
                          onChange={(e) => setFieldMappings(prev => ({
                            ...prev, [cf.key || cf.id]: e.target.value
                          }))}
                          className="input text-sm py-1"
                        >
                          <option value="auto">Auto-create field</option>
                          <option value="skip">Skip</option>
                          <option value="custom_text">Map to Text Field</option>
                          <option value="custom_number">Map to Number Field</option>
                          <option value="custom_select">Map to Select Field</option>
                          <option value="custom_date">Map to Date Field</option>
                        </select>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex justify-between">
            <button onClick={() => setCurrentStep('upload')} className="btn">
              Back
            </button>
            <button onClick={() => setCurrentStep('preview')} className="btn-primary flex items-center gap-2">
              Continue <ArrowRight size={16} />
            </button>
          </div>
        </div>
      )}

      {/* Step 3: Preview */}
      {currentStep === 'preview' && (
        <div className="space-y-6">
          <div className="card p-6">
            <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">Import Preview</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              Review the data that will be imported
            </p>

            <div className="space-y-3">
              {JIRA_ENTITY_TYPES.filter(t => selectedEntities[t.key]).map(entity => {
                const count = parsedData?.[entity.key]?.length || parsedData?.[entity.key.replace('_', '')]?.length || 0
                return (
                  <div key={entity.key} className="flex items-center justify-between py-3 border-b border-gray-100 dark:border-gray-800 last:border-0">
                    <div className="flex items-center gap-3">
                      <span className="text-lg">{entity.icon}</span>
                      <span className="font-medium text-gray-900 dark:text-gray-100">{entity.label}</span>
                    </div>
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      {count} {count === 1 ? 'item' : 'items'}
                    </span>
                  </div>
                )
              })}
            </div>

            <div className="mt-6 p-4 bg-yellow-50 dark:bg-yellow-900/20 rounded-lg">
              <div className="flex items-start gap-2">
                <AlertTriangle size={16} className="text-yellow-600 dark:text-yellow-400 mt-0.5 flex-shrink-0" />
                <div>
                  <p className="text-sm font-medium text-yellow-800 dark:text-yellow-200">
                    Important Notes
                  </p>
                  <ul className="text-sm text-yellow-700 dark:text-yellow-300 mt-1 space-y-1 list-disc list-inside">
                    <li>Existing data will not be overwritten — duplicates are skipped</li>
                    <li>User references will be mapped by email address</li>
                    <li>Attachment files are not included in JSON exports</li>
                    <li>Workflow transitions will be created based on status mappings</li>
                  </ul>
                </div>
              </div>
            </div>
          </div>

          <div className="flex justify-between">
            <button onClick={() => setCurrentStep('mapping')} className="btn">
              Back
            </button>
            <button
              onClick={handleStartImport}
              className="btn bg-green-600 text-white hover:bg-green-700 flex items-center gap-2"
            >
              Start Import <ArrowRight size={16} />
            </button>
          </div>
        </div>
      )}

      {/* Step 4: Importing */}
      {currentStep === 'import' && (
        <div className="card p-8 text-center">
          <Loader2 size={48} className="mx-auto text-brand-600 dark:text-brand-400 animate-spin mb-4" />
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
            Importing Data...
          </h3>
          <p className="text-gray-500 dark:text-gray-400 mb-6">
            This may take a few minutes depending on the amount of data.
          </p>
          {importProgress && (
            <div className="max-w-md mx-auto">
              <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3">
                <div
                  className="bg-brand-600 h-3 rounded-full transition-all duration-500"
                  style={{ width: `${Math.min(100, (importProgress.current / importProgress.total) * 100)}%` }}
                />
              </div>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
                {importProgress.current} of {importProgress.total} entity types processed
              </p>
            </div>
          )}
        </div>
      )}

      {/* Step 5: Complete */}
      {currentStep === 'complete' && importResult && (
        <div className="space-y-6">
          <div className={cn(
            'card p-8 text-center',
            importResult.status === 'failed'
              ? 'border-red-200 dark:border-red-800'
              : 'border-green-200 dark:border-green-800'
          )}>
            {importResult.status === 'failed' ? (
              <>
                <XCircle size={48} className="mx-auto text-red-500 mb-4" />
                <h3 className="text-lg font-semibold text-red-700 dark:text-red-300 mb-2">
                  Import Failed
                </h3>
                <p className="text-gray-500 dark:text-gray-400">
                  {importResult.error || 'An error occurred during import.'}
                </p>
              </>
            ) : (
              <>
                <CheckCircle size={48} className="mx-auto text-green-500 mb-4" />
                <h3 className="text-lg font-semibold text-green-700 dark:text-green-300 mb-2">
                  Import Complete
                </h3>
                <p className="text-gray-500 dark:text-gray-400">
                  Your Jira data has been successfully imported into Pulse.
                </p>
              </>
            )}
          </div>

          {importResult.summary && (
            <div className="card p-6">
              <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">Import Summary</h3>
              <div className="space-y-2">
                {Object.entries(importResult.summary).map(([entity, stats]) => (
                  <div key={entity} className="flex items-center justify-between py-2 border-b border-gray-100 dark:border-gray-800 last:border-0">
                    <span className="font-medium text-gray-900 dark:text-gray-100 capitalize">
                      {entity.replace(/_/g, ' ')}
                    </span>
                    <div className="flex items-center gap-3 text-sm">
                      {stats.created > 0 && (
                        <span className="text-green-600 dark:text-green-400">{stats.created} created</span>
                      )}
                      {stats.skipped > 0 && (
                        <span className="text-yellow-600 dark:text-yellow-400">{stats.skipped} skipped</span>
                      )}
                      {stats.errors > 0 && (
                        <span className="text-red-600 dark:text-red-400">{stats.errors} errors</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="flex justify-center">
            <button
              onClick={() => {
                setCurrentStep('upload')
                setImportFile(null)
                setParsedData(null)
                setImportResult(null)
                setErrors([])
              }}
              className="btn-primary"
            >
              Import Another Project
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
