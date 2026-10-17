import { useState, useCallback, useMemo, useRef } from 'react'

/**
 * Hook for managing multi-select state with support for:
 * - Individual toggle
 * - Select/deselect all
 * - Shift-click range selection
 */
export function useMultiSelect(items = []) {
  const [selected, setSelected] = useState(new Set())
  const lastSelectedRef = useRef(null)

  // Get item IDs for lookup
  const itemIds = useMemo(() => items.map(item => item.id), [items])
  const itemIdSet = useMemo(() => new Set(itemIds), [itemIds])

  // Toggle a single item's selection
  const toggle = useCallback((id) => {
    setSelected(prev => {
      const newSelected = new Set(prev)
      if (newSelected.has(id)) {
        newSelected.delete(id)
      } else {
        newSelected.add(id)
      }
      lastSelectedRef.current = id
      return newSelected
    })
  }, [])

  // Toggle with shift-click range selection
  const toggleWithShift = useCallback((id, shiftKey) => {
    if (!shiftKey || lastSelectedRef.current === null) {
      toggle(id)
      return
    }

    // Find range between last selected and current
    const lastIndex = itemIds.indexOf(lastSelectedRef.current)
    const currentIndex = itemIds.indexOf(id)

    if (lastIndex === -1 || currentIndex === -1) {
      toggle(id)
      return
    }

    const start = Math.min(lastIndex, currentIndex)
    const end = Math.max(lastIndex, currentIndex)

    setSelected(prev => {
      const newSelected = new Set(prev)
      for (let i = start; i <= end; i++) {
        newSelected.add(itemIds[i])
      }
      return newSelected
    })
    lastSelectedRef.current = id
  }, [itemIds, toggle])

  // Select all visible items
  const selectAll = useCallback(() => {
    setSelected(new Set(itemIds))
    if (itemIds.length > 0) {
      lastSelectedRef.current = itemIds[itemIds.length - 1]
    }
  }, [itemIds])

  // Deselect all
  const deselectAll = useCallback(() => {
    setSelected(new Set())
    lastSelectedRef.current = null
  }, [])

  // Check if an item is selected
  const isSelected = useCallback((id) => selected.has(id), [selected])

  // Get selected items (filtered by current items to handle stale selections)
  const selectedItems = useMemo(() => {
    return items.filter(item => selected.has(item.id))
  }, [items, selected])

  // Get array of selected IDs (only those that are in current items)
  const selectedIds = useMemo(() => {
    return itemIds.filter(id => selected.has(id))
  }, [itemIds, selected])

  // Count of selected items
  const selectedCount = selectedIds.length

  // Check if all items are selected
  const allSelected = useMemo(() => {
    return itemIds.length > 0 && itemIds.every(id => selected.has(id))
  }, [itemIds, selected])

  // Check if some (but not all) items are selected
  const someSelected = useMemo(() => {
    return selectedCount > 0 && !allSelected
  }, [selectedCount, allSelected])

  // Toggle all - if all selected, deselect all; otherwise select all
  const toggleAll = useCallback(() => {
    if (allSelected) {
      deselectAll()
    } else {
      selectAll()
    }
  }, [allSelected, deselectAll, selectAll])

  return {
    selected,
    toggle,
    toggleWithShift,
    selectAll,
    deselectAll,
    isSelected,
    selectedItems,
    selectedIds,
    selectedCount,
    allSelected,
    someSelected,
    toggleAll,
  }
}

export default useMultiSelect
