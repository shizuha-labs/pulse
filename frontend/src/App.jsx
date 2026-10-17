import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, useLocation } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { OrganizationProvider } from './contexts/OrganizationContext'
import ProtectedRoute from './components/ProtectedRoute'
import Layout from './components/Layout'
import ErrorBoundary from './components/ErrorBoundary'
import { ToastProvider } from './components/Toast'

// PLAT-875: route-level code splitting. The shell (providers + ProtectedRoute +
// Layout) stays eager so the unauth/login redirect and first paint are lean;
// every page is lazy-loaded as its own chunk, so the ~1.8 MB monolithic initial
// bundle no longer ships all ~30 pages (incl. 11 rarely-used admin screens) up
// front. React.lazy is fail-safe: a route's chunk loads on navigation behind the
// Suspense fallback; a bad chunk only affects that route, never the whole app.
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Tasks = lazy(() => import('./pages/Tasks'))
const TaskDetail = lazy(() => import('./pages/TaskDetail'))
const TeamTasks = lazy(() => import('./pages/TeamTasks'))
const OrgChart = lazy(() => import('./pages/OrgChart'))
const AgentConsole = lazy(() => import('./pages/AgentConsole'))
const Actions = lazy(() => import('./pages/Actions'))
const Alerts = lazy(() => import('./pages/Alerts'))
const LaneHealth = lazy(() => import('./pages/LaneHealth'))
const Rules = lazy(() => import('./pages/Rules'))
const Settings = lazy(() => import('./pages/Settings'))
const Projects = lazy(() => import('./pages/Projects'))
const ProjectBoard = lazy(() => import('./pages/ProjectBoard'))
const ProjectSettings = lazy(() => import('./pages/ProjectSettings'))
const Sprints = lazy(() => import('./pages/Sprints'))
const SprintBoard = lazy(() => import('./pages/SprintBoard'))
const Backlog = lazy(() => import('./pages/Backlog'))
const Calendar = lazy(() => import('./pages/Calendar'))
const Roadmap = lazy(() => import('./pages/Roadmap'))
const Notifications = lazy(() => import('./pages/Notifications'))
const Archive = lazy(() => import('./pages/Archive'))
const CustomerPortal = lazy(() => import('./pages/CustomerPortal'))
const Analytics = lazy(() => import('./pages/Analytics'))

// Admin pages — heavy + rarely used by most customers; always lazy.
const Administration = lazy(() => import('./pages/admin/Administration'))
const IssueTypes = lazy(() => import('./pages/admin/IssueTypes'))
const CustomFields = lazy(() => import('./pages/admin/CustomFields'))
const Screens = lazy(() => import('./pages/admin/Screens'))
const SecuritySchemes = lazy(() => import('./pages/admin/SecuritySchemes'))
const PermissionSchemes = lazy(() => import('./pages/admin/PermissionSchemes'))
const NotificationSchemes = lazy(() => import('./pages/admin/NotificationSchemes'))
const SLAManagement = lazy(() => import('./pages/admin/SLAManagement'))
const Automation = lazy(() => import('./pages/admin/Automation'))
const BoardsAdmin = lazy(() => import('./pages/admin/BoardsAdmin'))
const DashboardsAdmin = lazy(() => import('./pages/admin/DashboardsAdmin'))
const JiraImport = lazy(() => import('./pages/admin/JiraImport'))

// Chat widget is rendered on every page but isn't needed for first paint — lazy
// it so its (heavy) bundle loads after the shell, with a null fallback.
const ShizuhaChatWidget = lazy(() =>
  import('./components/chat').then((m) => ({ default: m.ShizuhaChatWidget }))
)

// Base path for router - matches Vite's base config
// When deployed under /pulse/, router needs to know about it
const basename = import.meta.env.BASE_URL?.replace(/\/$/, '') || ''

// Redirect to Shizuha ID login with continue URL
function RedirectToLogin() {
  window.location.href = `/id/login?continue=${encodeURIComponent('/pulse/')}`
  return null
}

// Redirect to Shizuha ID register with continue URL
function RedirectToRegister() {
  window.location.href = `/id/register?continue=${encodeURIComponent('/pulse/')}`
  return null
}

function RouteFallback() {
  return <div className="p-8 text-center text-gray-500 dark:text-gray-400">Loading…</div>
}

// PLS-915: key the ErrorBoundary by the current route so navigating to a
// different page resets a caught chunk/render error instead of showing a
// stale failure. Must live inside <BrowserRouter> to read useLocation().
function RoutedContent() {
  const location = useLocation()
  return (
    <ErrorBoundary key={location.pathname}>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          {/* Redirect old login/register routes to Shizuha ID */}
          <Route path="/login" element={<RedirectToLogin />} />
          <Route path="/register" element={<RedirectToRegister />} />

          {/* Public customer request portal */}
          <Route path="/portal/:projectRef" element={<CustomerPortal />} />

          {/* Protected routes */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Dashboard />} />
            <Route path="tasks" element={<Tasks />} />
            <Route path="tasks/:taskKey" element={<TaskDetail />} />
            <Route path="archive" element={<Archive />} />
            <Route path="browse/:taskKey" element={<TaskDetail />} />
            <Route path="team" element={<TeamTasks />} />
            <Route path="org-chart" element={<OrgChart />} />
            <Route path="agents" element={<AgentConsole />} />
            <Route path="projects" element={<Projects />} />
            <Route path="projects/:projectId" element={<ProjectBoard />} />
            <Route path="projects/:projectId/settings" element={<ProjectSettings />} />
            <Route path="projects/:projectId/sprints" element={<Sprints />} />
            <Route path="projects/:projectId/sprints/:sprintId" element={<SprintBoard />} />
            <Route path="projects/:projectId/backlog" element={<Backlog />} />
            <Route path="calendar" element={<Calendar />} />
            <Route path="roadmap" element={<Roadmap />} />
            <Route path="analytics" element={<Analytics />} />
            <Route path="notifications" element={<Notifications />} />
            <Route path="actions" element={<Actions />} />
            <Route path="alerts" element={<Alerts />} />
            <Route path="lane-health" element={<LaneHealth />} />
            <Route path="rules" element={<Rules />} />

            {/* Administration routes */}
            <Route path="admin" element={<Administration />} />
            <Route path="admin/issue-types" element={<IssueTypes />} />
            <Route path="admin/custom-fields" element={<CustomFields />} />
            <Route path="admin/screens" element={<Screens />} />
            <Route path="admin/security-schemes" element={<SecuritySchemes />} />
            <Route path="admin/permission-schemes" element={<PermissionSchemes />} />
            <Route path="admin/notification-schemes" element={<NotificationSchemes />} />
            <Route path="admin/sla" element={<SLAManagement />} />
            <Route path="admin/automation" element={<Automation />} />
            <Route path="admin/boards" element={<BoardsAdmin />} />
            <Route path="admin/dashboards" element={<DashboardsAdmin />} />
            <Route path="admin/jira-import" element={<JiraImport />} />

            <Route path="settings" element={<Settings />} />
          </Route>
        </Routes>
      </Suspense>
    </ErrorBoundary>
  )
}

export default function App() {
  return (
    <BrowserRouter
      basename={basename}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <ToastProvider>
      <AuthProvider>
        <OrganizationProvider>
          <RoutedContent />
          <Suspense fallback={null}>
            <ShizuhaChatWidget
              sourceService="pulse"
              welcomeMessage="Hi! I can help you manage tasks, create items, search your backlog, and answer questions about Pulse."
              showToolCalls={true}
            />
          </Suspense>
        </OrganizationProvider>
      </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  )
}
