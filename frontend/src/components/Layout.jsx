import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  Activity,
  LayoutDashboard,
  CheckSquare,
  Calendar,
  Bell,
  Zap,
  AlertTriangle,
  Settings,
  Shield,
  Menu,
  X,
  LogOut,
  User,
  Users,
  Network,
  ChevronDown,
  ChevronRight,
  FolderKanban,
  HeartPulse,
  Building2,
  Layers,
  Wrench,
  Archive as ArchiveIcon,
  BarChart3,
  Map,
} from 'lucide-react'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { cn } from '../utils/cn'
import ThemeToggle from './ThemeToggle'
import AppSwitcher from './AppSwitcher'
import { useEnabledServices } from '@shizuha/ui'
import { useAuth } from '../contexts/AuthContext'
import { useOrganization } from '../contexts/OrganizationContext'
import { notificationsApi } from '../services/api'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/tasks', icon: CheckSquare, label: 'Tasks' },
  { to: '/notifications', icon: Bell, label: 'Notifications', badge: true },
  { to: '/calendar', icon: Calendar, label: 'Calendar' },
  { to: '/team', icon: Users, label: 'Team Tasks' },
  { to: '/org-chart', icon: Network, label: 'Org Chart' },
  { to: '/agents', icon: Activity, label: 'Agent Console' },
  { to: '/projects', icon: FolderKanban, label: 'Projects' },
  { to: '/roadmap', icon: Map, label: 'Roadmap' },
  { to: '/analytics', icon: BarChart3, label: 'Analytics' },
  { to: '/actions', icon: Zap, label: 'Actions' },
  { to: '/alerts', icon: AlertTriangle, label: 'Alerts' },
  { to: '/lane-health', icon: HeartPulse, label: 'Lane Health' },
  { to: '/rules', icon: Shield, label: 'Rules' },
  { to: '/archive', icon: ArchiveIcon, label: 'Archive' },
  { to: '/admin', icon: Wrench, label: 'Administration' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

export default function Layout() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [orgMenuOpen, setOrgMenuOpen] = useState(false)
  const { user, logout } = useAuth()
  const { organizations, currentOrganization, switchOrganization } = useOrganization()
  const navigate = useNavigate()
  const enabledServices = useEnabledServices()

  // Poll unread notification count every 30s
  const { data: unreadData } = useQuery({
    queryKey: ['unreadCount'],
    queryFn: () => notificationsApi.unreadCount().then(r => r.data),
    refetchInterval: 30000,
  })
  const unreadCount = unreadData?.count || 0

  const handleLogout = async () => {
    await logout()
    navigate('/login')
  }

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950">
      {/* Mobile header */}
      <header className="lg:hidden fixed top-0 left-0 right-0 z-50 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800">
        <div className="flex items-center justify-between px-4 h-14">
          <div className="flex items-center gap-2">
              <AppSwitcher currentAppId="pulse" enabledServices={enabledServices} />
              <HeartPulse size={20} className="text-brand-600 dark:text-brand-400 flex-shrink-0" />
              <h1 className="text-lg font-semibold text-brand-600 dark:text-brand-400 whitespace-nowrap">Pulse</h1>
              <span className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-24">
                {currentOrganization ? currentOrganization.name : 'All Organizations'}
              </span>
            </div>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <button
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className="btn-icon"
              aria-label="Toggle menu"
            >
              {mobileMenuOpen ? <X size={24} /> : <Menu size={24} />}
            </button>
          </div>
        </div>
      </header>

      {/* Mobile menu overlay */}
      {mobileMenuOpen && (
        <div
          className="lg:hidden fixed inset-0 z-40 bg-black/50 dark:bg-black/70"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={cn(
          'fixed top-0 left-0 z-50 lg:z-30 h-full w-64 bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 transform transition-transform duration-200 ease-in-out',
          'lg:translate-x-0',
          mobileMenuOpen ? 'translate-x-0' : '-translate-x-full'
        )}
      >
        <div className="flex flex-col h-full">
          {/* Logo with App Switcher */}
          <div className="h-14 lg:h-16 flex items-center justify-between px-4 border-b border-gray-200 dark:border-gray-800">
            <div className="flex items-center gap-3 min-w-0">
              <AppSwitcher currentAppId="pulse" enabledServices={enabledServices} />
              <div className="flex items-center gap-2 min-w-0">
                <HeartPulse size={22} className="text-brand-600 dark:text-brand-400 flex-shrink-0" />
                <h1 className="text-xl font-bold text-brand-600 dark:text-brand-400 whitespace-nowrap">Pulse</h1>
              </div>
            </div>
            <div className="hidden lg:block">
              <ThemeToggle />
            </div>
          </div>

          {/* Organization switcher */}
          {organizations.length > 0 && (
            <div className="px-3 py-2 border-b border-gray-200 dark:border-gray-800">
              <div className="relative">
                <button
                  onClick={() => setOrgMenuOpen(!orgMenuOpen)}
                  className="w-full flex items-center gap-2 px-2 py-1.5 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-md transition-colors"
                >
                  {currentOrganization
                    ? (currentOrganization.is_personal ? <User size={16} className="flex-shrink-0" /> : <Building2 size={16} className="flex-shrink-0" />)
                    : <Layers size={16} className="flex-shrink-0" />
                  }
                  <span className="truncate flex-1 text-left">{currentOrganization?.name || 'All Organizations'}</span>
                  <ChevronDown size={14} className={cn('text-gray-400 transition-transform flex-shrink-0', orgMenuOpen && 'rotate-180')} />
                </button>

                {orgMenuOpen && (
                  <>
                    <div className="fixed inset-0 z-10" onClick={() => setOrgMenuOpen(false)} />
                    <div className="absolute left-0 right-0 top-full mt-1 z-20 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg overflow-hidden">
                      <div className="px-3 py-1.5 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                        Switch Organization
                      </div>
                      {/* All Organizations option */}
                      <button
                        onClick={() => {
                          switchOrganization(null)
                          setOrgMenuOpen(false)
                        }}
                        className={cn(
                          'flex items-center w-full px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700',
                          currentOrganization === null
                            ? 'bg-brand-50 dark:bg-brand-950 text-brand-700 dark:text-brand-300'
                            : 'text-gray-700 dark:text-gray-300'
                        )}
                      >
                        <Layers size={14} className="mr-2 flex-shrink-0" />
                        <span className="truncate">All Organizations</span>
                        {currentOrganization === null && (
                          <span className="ml-auto text-brand-500 flex-shrink-0">✓</span>
                        )}
                      </button>
                      {/* Individual orgs */}
                      {organizations.map((org) => (
                        <button
                          key={org.id}
                          onClick={() => {
                            switchOrganization(org)
                            setOrgMenuOpen(false)
                          }}
                          className={cn(
                            'flex items-center w-full px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-700',
                            currentOrganization?.id === org.id
                              ? 'bg-brand-50 dark:bg-brand-950 text-brand-700 dark:text-brand-300'
                              : 'text-gray-700 dark:text-gray-300'
                          )}
                        >
                          {org.is_personal ? <User size={14} className="mr-2 flex-shrink-0" /> : <Building2 size={14} className="mr-2 flex-shrink-0" />}
                          <span className="truncate">{org.name}</span>
                          {currentOrganization?.id === org.id && (
                            <span className="ml-auto text-brand-500 flex-shrink-0">✓</span>
                          )}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>
          )}

          {/* Navigation */}
          <nav className="flex-1 overflow-y-auto py-4">
            <ul className="space-y-1 px-2">
              {navItems.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    onClick={() => setMobileMenuOpen(false)}
                    className={({ isActive }) =>
                      cn(
                        'flex items-center gap-3 px-3 py-2.5 rounded-lg transition-colors',
                        isActive
                          ? 'bg-brand-50 text-brand-700 font-medium dark:bg-brand-950 dark:text-brand-300'
                          : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                      )
                    }
                  >
                    <div className="relative">
                      <item.icon size={20} />
                      {item.badge && unreadCount > 0 && (
                        <span className="absolute -top-1.5 -right-1.5 min-w-[18px] h-[18px] rounded-full bg-red-500 text-white text-[10px] font-bold flex items-center justify-center px-1">
                          {unreadCount > 99 ? '99+' : unreadCount}
                        </span>
                      )}
                    </div>
                    <span>{item.label}</span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>

          {/* User section */}
          <div className="p-3 border-t border-gray-200 dark:border-gray-800">
            <div className="relative">
              <button
                onClick={() => setUserMenuOpen(!userMenuOpen)}
                className="w-full flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
              >
                <div className="w-8 h-8 rounded-full bg-brand-100 dark:bg-brand-900 flex items-center justify-center">
                  <User size={16} className="text-brand-600 dark:text-brand-400" />
                </div>
                <div className="flex-1 text-left min-w-0">
                  <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                    {user?.first_name || user?.username || 'User'}
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-400 truncate">
                    {user?.email}
                  </p>
                </div>
                <ChevronDown size={16} className={cn(
                  'text-gray-400 transition-transform',
                  userMenuOpen && 'rotate-180'
                )} />
              </button>

              {/* User dropdown menu */}
              {userMenuOpen && (
                <>
                  <div
                    className="fixed inset-0 z-10"
                    onClick={() => setUserMenuOpen(false)}
                  />
                  <div className="absolute bottom-full left-0 right-0 mb-1 z-20 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg overflow-hidden">
                    <button
                      onClick={() => {
                        setUserMenuOpen(false)
                        handleLogout()
                      }}
                      className="w-full flex items-center gap-3 px-4 py-3 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
                    >
                      <LogOut size={16} />
                      <span>Sign out</span>
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="lg:ml-64 pt-14 lg:pt-0 min-h-screen pb-16 lg:pb-0">
        <div className="p-4 lg:p-6">
          <Outlet />
        </div>
      </main>

      {/* Mobile bottom navigation */}
      <nav className="lg:hidden fixed bottom-0 left-0 right-0 z-40 bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-800 safe-area-inset-bottom">
        <div className="flex justify-around">
          {navItems.slice(0, 5).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  'flex flex-col items-center py-2 px-3 min-w-0 transition-colors',
                  isActive
                    ? 'text-brand-600 dark:text-brand-400'
                    : 'text-gray-500 dark:text-gray-400'
                )
              }
            >
              <div className="relative">
                <item.icon size={20} />
                {item.badge && unreadCount > 0 && (
                  <span className="absolute -top-1 -right-2 min-w-[16px] h-[16px] rounded-full bg-red-500 text-white text-[9px] font-bold flex items-center justify-center px-0.5">
                    {unreadCount > 99 ? '99+' : unreadCount}
                  </span>
                )}
              </div>
              <span className="text-xs mt-1 truncate">{item.label}</span>
            </NavLink>
          ))}
        </div>
      </nav>
    </div>
  )
}
