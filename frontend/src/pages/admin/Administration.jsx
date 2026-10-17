import { useNavigate } from 'react-router-dom'
import {
  Settings, Bug, Layers, Monitor, Shield, Key, Bell, Clock, Zap,
  LayoutDashboard, Kanban, FileText, ChevronRight
} from 'lucide-react'
import { cn } from '../../utils/cn'

const categories = [
  {
    title: 'Issue Management',
    cards: [
      {
        title: 'Issue Types',
        description: 'Define and manage issue types and issue type schemes',
        icon: Bug,
        path: '/admin/issue-types',
      },
      {
        title: 'Custom Fields',
        description: 'Create custom fields for issues',
        icon: FileText,
        path: '/admin/custom-fields',
      },
      {
        title: 'Screens',
        description: 'Configure field layouts for create, edit, and view operations',
        icon: Monitor,
        path: '/admin/screens',
      },
    ],
  },
  {
    title: 'Schemes & Configuration',
    cards: [
      {
        title: 'Security Schemes',
        description: 'Manage issue-level security and access control',
        icon: Shield,
        path: '/admin/security-schemes',
      },
      {
        title: 'Permission Schemes',
        description: 'Define project permissions and role-based access',
        icon: Key,
        path: '/admin/permission-schemes',
      },
      {
        title: 'Notification Schemes',
        description: 'Configure who gets notified for what events',
        icon: Bell,
        path: '/admin/notification-schemes',
      },
    ],
  },
  {
    title: 'Operations',
    cards: [
      {
        title: 'SLA Management',
        description: 'Define service level agreements and business calendars',
        icon: Clock,
        path: '/admin/sla',
      },
      {
        title: 'Automation',
        description: 'Create rules to automate repetitive tasks',
        icon: Zap,
        path: '/admin/automation',
      },
      {
        title: 'Boards',
        description: 'Manage boards, swimlanes, and quick filters',
        icon: Kanban,
        path: '/admin/boards',
      },
      {
        title: 'Dashboards',
        description: 'Create and configure dashboards with gadgets',
        icon: LayoutDashboard,
        path: '/admin/dashboards',
      },
    ],
  },
]

function AdminCard({ title, description, icon: Icon, path, navigate }) {
  return (
    <button
      onClick={() => navigate(path)}
      className="card-hover p-6 text-left flex items-start gap-4 group"
    >
      <div className="p-3 rounded-lg bg-brand-50 dark:bg-brand-950 text-brand-600 dark:text-brand-400 group-hover:bg-brand-100 dark:group-hover:bg-brand-900 transition-colors">
        <Icon size={24} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
          <ChevronRight size={16} className="text-gray-400 group-hover:text-brand-500 transition-colors" />
        </div>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{description}</p>
      </div>
    </button>
  )
}

export default function Administration() {
  const navigate = useNavigate()

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Administration</h1>
        <p className="text-gray-500 dark:text-gray-400">
          Manage global settings, schemes, and configurations
        </p>
      </div>

      {categories.map((category) => (
        <div key={category.title}>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">
            {category.title}
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {category.cards.map((card) => (
              <AdminCard
                key={card.path}
                title={card.title}
                description={card.description}
                icon={card.icon}
                path={card.path}
                navigate={navigate}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
