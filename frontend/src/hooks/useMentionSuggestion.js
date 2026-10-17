import { ReactRenderer } from '@tiptap/react'
import tippy from 'tippy.js'
import MentionList from '../components/MentionList'

export function createMentionSuggestion(getItems) {
  return {
    items: async ({ query }) => {
      const allItems = await getItems()
      if (!query) return allItems.slice(0, 5)

      const lowerQuery = query.toLowerCase()
      return allItems
        .filter(item =>
          item.label.toLowerCase().includes(lowerQuery) ||
          (item.email && item.email.toLowerCase().includes(lowerQuery))
        )
        .slice(0, 5)
    },

    render: () => {
      let component
      let popup

      return {
        onStart: (props) => {
          component = new ReactRenderer(MentionList, {
            props,
            editor: props.editor,
          })

          if (!props.clientRect) {
            return
          }

          popup = tippy('body', {
            getReferenceClientRect: props.clientRect,
            appendTo: () => document.body,
            content: component.element,
            showOnCreate: true,
            interactive: true,
            trigger: 'manual',
            placement: 'bottom-start',
          })
        },

        onUpdate(props) {
          component?.updateProps(props)

          if (!props.clientRect) {
            return
          }

          popup?.[0]?.setProps({
            getReferenceClientRect: props.clientRect,
          })
        },

        onKeyDown(props) {
          if (props.event.key === 'Escape') {
            popup?.[0]?.hide()
            return true
          }

          return component?.ref?.onKeyDown(props)
        },

        onExit() {
          popup?.[0]?.destroy()
          component?.destroy()
        },
      }
    },
  }
}

// Simplified mention configuration that can work without tippy.js
export function createSimpleMentionSuggestion(getItems) {
  return {
    items: async ({ query }) => {
      const allItems = await getItems()
      if (!query) return allItems.slice(0, 5)

      const lowerQuery = query.toLowerCase()
      return allItems
        .filter(item =>
          item.label.toLowerCase().includes(lowerQuery) ||
          (item.email && item.email.toLowerCase().includes(lowerQuery))
        )
        .slice(0, 5)
    },

    // Allow the extension to render inline
    allowSpaces: false,

    render: () => {
      let component
      let popup

      return {
        onStart: (props) => {
          component = new ReactRenderer(MentionList, {
            props,
            editor: props.editor,
          })

          if (!props.clientRect) {
            return
          }

          popup = tippy('body', {
            getReferenceClientRect: props.clientRect,
            appendTo: () => document.body,
            content: component.element,
            showOnCreate: true,
            interactive: true,
            trigger: 'manual',
            placement: 'bottom-start',
            maxWidth: 'none',
          })
        },

        onUpdate(props) {
          component?.updateProps(props)

          if (!props.clientRect) {
            return
          }

          popup?.[0]?.setProps({
            getReferenceClientRect: props.clientRect,
          })
        },

        onKeyDown(props) {
          if (props.event.key === 'Escape') {
            popup?.[0]?.hide()
            return true
          }

          return component?.ref?.onKeyDown(props)
        },

        onExit() {
          popup?.[0]?.destroy()
          component?.destroy()
        },
      }
    },
  }
}
