// Pure navigation-guard decision logic, kept framework-free for unit tests.

export const PUBLIC_ROUTE_NAMES = new Set(['login', 'register'])

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
  const { hasToken, routeName, fullPath = '/' } = decision
  if (!hasToken && !PUBLIC_ROUTE_NAMES.has(routeName)) {
    return { name: 'login', query: { next: fullPath } }
  }
  if (hasToken && PUBLIC_ROUTE_NAMES.has(routeName)) {
    return { name: 'home' }
  }
  return null
}
