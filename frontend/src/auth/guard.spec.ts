import { describe, expect, it } from 'vitest'

import {
  AUTH_FORMS_ROUTE_NAMES,
  AUTH_FREE_ROUTE_NAMES,
  authRedirectDecision,
} from './guard'

describe('authRedirectDecision', () => {
  it('sends anonymous users to login for protected routes with a next target', () => {
    expect(
      authRedirectDecision({ routeName: 'corpus', hasToken: false, fullPath: '/corpus' }),
    ).toEqual({ name: 'login', query: { next: '/corpus' } })
    expect(
      authRedirectDecision({
        routeName: 'corpus-instrument',
        hasToken: false,
        fullPath: '/corpus/abc',
      }),
    ).toEqual({ name: 'login', query: { next: '/corpus/abc' } })
  })

  it('lets anonymous users browse auth-free product surfaces', () => {
    for (const routeName of [...AUTH_FREE_ROUTE_NAMES]) {
      expect(authRedirectDecision({ routeName, hasToken: false })).toBeNull()
    }
  })

  it('lets anonymous users reach the auth form pages', () => {
    for (const routeName of [...AUTH_FORMS_ROUTE_NAMES]) {
      expect(authRedirectDecision({ routeName, hasToken: false })).toBeNull()
    }
  })

  it('sends authenticated users away from auth forms to the conversation', () => {
    expect(authRedirectDecision({ routeName: 'login', hasToken: true })).toEqual({
      name: 'chat',
    })
    expect(authRedirectDecision({ routeName: 'register', hasToken: true })).toEqual({
      name: 'chat',
    })
  })

  it('keeps authenticated users on product surfaces instead of bouncing them', () => {
    for (const routeName of ['home', 'chat', 'corpus', 'not-found']) {
      expect(authRedirectDecision({ routeName, hasToken: true })).toBeNull()
    }
  })
})
