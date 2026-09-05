import { describe, expect, it } from 'vitest'

import { authRedirectDecision } from './guard'

describe('authRedirectDecision', () => {
  it('redirects anonymous users to login with a next target', () => {
    expect(authRedirectDecision({ routeName: 'corpus', hasToken: false, fullPath: '/corpus' })).toEqual(
      { name: 'login', query: { next: '/corpus' } },
    )
  })

  it('sends authenticated users away from login to home', () => {
    expect(authRedirectDecision({ routeName: 'login', hasToken: true })).toEqual({ name: 'home' })
  })

  it('sends authenticated users away from register to home', () => {
    expect(authRedirectDecision({ routeName: 'register', hasToken: true })).toEqual({ name: 'home' })
  })

  it('lets authenticated users reach protected routes', () => {
    expect(authRedirectDecision({ routeName: 'corpus', hasToken: true })).toBeNull()
  })

  it('lets anonymous users reach public routes', () => {
    expect(authRedirectDecision({ routeName: 'login', hasToken: false })).toBeNull()
    expect(authRedirectDecision({ routeName: 'register', hasToken: false })).toBeNull()
  })
})
