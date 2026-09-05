// Pure navigation-guard decision logic, kept framework-free for unit tests.

// Anyone (logged-in or not) may visit these routes.
export const AUTH_FREE_ROUTE_NAMES = new Set(['home', 'chat', 'not-found'])
// Only meaningful for anonymous visitors: these pages host the login flow.
export const AUTH_FORMS_ROUTE_NAMES = new Set(['login', 'register'])

export interface AuthRedirect {
  name: string
  query?: { next?: string }
}

export interface AuthRedirectDecision {
  hasToken: boolean
  routeName: string
  fullPath?: string
}

export function authRedirectDecision(
  decision: AuthRedirectDecision,
): AuthRedirect | null {
  const { hasToken, routeName, fullPath = '/chat' } = decision
  const openToAll = AUTH_FREE_ROUTE_NAMES.has(routeName)
  const authForm = AUTH_FORMS_ROUTE_NAMES.has(routeName)
  if (!hasToken && !openToAll && !authForm) {
    return { name: 'login', query: { next: fullPath } }
  }
  if (hasToken && authForm) {
    return { name: 'chat' }
  }
  return null
}
