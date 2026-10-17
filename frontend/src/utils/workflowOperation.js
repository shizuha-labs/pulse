// HTTP202 records intent, not completion. Keep task truth unchanged until commit.
export function workflowPendingMessage(response) {
  return response?.status === 202 && response?.data?.pending === true
    ? 'Transition queued. The task remains in its current status until processing finishes.'
    : null
}
