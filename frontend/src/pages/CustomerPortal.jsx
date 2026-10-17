import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { customerPortalApi } from '../services/api'

const statusLabels = {
  submitted: 'Submitted',
  under_review: 'Under review',
  planned: 'Planned',
  in_progress: 'In progress',
  shipped: 'Shipped',
  declined: 'Declined',
}

function RequestCard({ projectRef, request, onVoted }) {
  const [voting, setVoting] = useState(false)
  const [error, setError] = useState('')

  const vote = async () => {
    setVoting(true)
    setError('')
    try {
      const response = await customerPortalApi.vote(projectRef, request.id)
      onVoted(request.id, response.data.upvote_count)
    } catch (err) {
      setError(err.response?.data?.detail || 'Could not record vote')
    } finally {
      setVoting(false)
    }
  }

  return (
    <article className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-800">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{request.title}</h3>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
            <span className="rounded-full bg-blue-50 px-2 py-0.5 text-blue-700 dark:bg-blue-900/40 dark:text-blue-200">
              {statusLabels[request.status] || request.status}
            </span>
            {request.category ? <span>{request.category}</span> : null}
            {request.created_item_key ? <span>Tracked as {request.created_item_key}</span> : null}
          </div>
        </div>
        <button
          type="button"
          onClick={vote}
          disabled={voting}
          className="rounded-lg border border-blue-200 px-3 py-2 text-sm font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-60 dark:border-blue-700 dark:text-blue-200 dark:hover:bg-blue-900/40"
        >
          ▲ {request.upvote_count}
        </button>
      </div>
      <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-gray-700 dark:text-gray-300">{request.description}</p>
      {request.requester_name ? <p className="mt-3 text-xs text-gray-500">Requested by {request.requester_name}</p> : null}
      {error ? <p className="mt-2 text-sm text-red-600 dark:text-red-400">{error}</p> : null}
    </article>
  )
}

export default function CustomerPortal() {
  const { projectRef } = useParams()
  const [project, setProject] = useState(null)
  const [requests, setRequests] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [form, setForm] = useState({ title: '', description: '', category: '', requester_name: '', requester_email: '' })
  const [submitting, setSubmitting] = useState(false)
  const categories = useMemo(() => Array.from(new Set(requests.map((r) => r.category).filter(Boolean))).sort(), [requests])

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const response = await customerPortalApi.list(projectRef)
      setProject(response.data.project)
      setRequests(response.data.results || [])
    } catch (err) {
      setError(err.response?.data?.detail || 'Could not load request portal')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [projectRef])

  const update = (event) => setForm({ ...form, [event.target.name]: event.target.value })

  const submit = async (event) => {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      const response = await customerPortalApi.submit(projectRef, form)
      setRequests([response.data, ...requests])
      setForm({ title: '', description: '', category: '', requester_name: '', requester_email: '' })
    } catch (err) {
      const data = err.response?.data
      setError(typeof data === 'string' ? data : data?.detail || Object.values(data || {})[0]?.[0] || 'Could not submit request')
    } finally {
      setSubmitting(false)
    }
  }

  const onVoted = (id, upvoteCount) => {
    setRequests((current) => current.map((request) => (request.id === id ? { ...request, upvote_count: upvoteCount } : request)))
  }

  return (
    <main className="min-h-screen bg-gray-50 px-4 py-8 text-gray-900 dark:bg-gray-900 dark:text-gray-100">
      <div className="mx-auto max-w-5xl">
        <header className="mb-8">
          <p className="text-sm font-medium uppercase tracking-wide text-blue-600 dark:text-blue-300">Pulse customer requests</p>
          <h1 className="mt-2 text-3xl font-bold">{project ? `${project.name} feedback` : 'Customer request portal'}</h1>
          <p className="mt-2 max-w-2xl text-gray-600 dark:text-gray-300">
            Submit feature requests, vote on what matters, and track whether requests are submitted, planned, in progress, or shipped.
          </p>
        </header>

        <section className="mb-8 rounded-xl border border-gray-200 bg-white p-5 shadow-sm dark:border-gray-700 dark:bg-gray-800">
          <h2 className="text-xl font-semibold">Submit a request</h2>
          <form className="mt-4 grid gap-4" onSubmit={submit}>
            <input className="rounded-lg border border-gray-300 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-900" name="title" value={form.title} onChange={update} placeholder="Short title" required />
            <textarea className="min-h-28 rounded-lg border border-gray-300 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-900" name="description" value={form.description} onChange={update} placeholder="What problem should this solve?" required />
            <div className="grid gap-4 md:grid-cols-3">
              <input className="rounded-lg border border-gray-300 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-900" name="category" value={form.category} onChange={update} placeholder="Category" list="portal-categories" />
              <input className="rounded-lg border border-gray-300 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-900" name="requester_name" value={form.requester_name} onChange={update} placeholder="Your name (optional)" />
              <input className="rounded-lg border border-gray-300 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-900" name="requester_email" value={form.requester_email} onChange={update} placeholder="Email (optional)" type="email" />
            </div>
            <datalist id="portal-categories">{categories.map((category) => <option key={category} value={category} />)}</datalist>
            <button type="submit" disabled={submitting} className="w-fit rounded-lg bg-blue-600 px-4 py-2 font-medium text-white hover:bg-blue-700 disabled:opacity-60">
              {submitting ? 'Submitting…' : 'Submit request'}
            </button>
          </form>
        </section>

        {error ? <div className="mb-4 rounded-lg bg-red-50 p-3 text-red-700 dark:bg-red-900/30 dark:text-red-200">{error}</div> : null}
        {loading ? <div className="text-gray-500">Loading requests…</div> : null}
        {!loading && requests.length === 0 ? <div className="rounded-xl border border-dashed border-gray-300 p-8 text-center text-gray-500">No requests yet. Be the first to submit one.</div> : null}
        <div className="grid gap-4">
          {requests.map((request) => <RequestCard key={request.id} projectRef={projectRef} request={request} onVoted={onVoted} />)}
        </div>
      </div>
    </main>
  )
}
