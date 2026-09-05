import { createRouter, createWebHistory } from 'vue-router'

import { authRedirectDecision } from '../auth/guard'
import { session } from '../auth/session'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { title: '登录' },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('../views/RegisterView.vue'),
      meta: { title: '注册' },
    },
    {
      path: '/',
      component: () => import('../layouts/AppShell.vue'),
      children: [
        {
          path: '',
          name: 'home',
          component: () => import('../views/HomeView.vue'),
          meta: { title: '首页' },
        },
        {
          path: 'corpus',
          name: 'corpus',
          component: () => import('../views/CorpusView.vue'),
          meta: { title: '法规语料' },
        },
        {
          path: 'chat',
          name: 'chat',
          component: () => import('../views/ChatView.vue'),
          meta: { title: '法规对话' },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('../views/NotFoundView.vue'),
      meta: { title: '页面不存在' },
    },
  ],
})

router.beforeEach((to) => {
  const redirect = authRedirectDecision({
    routeName: String(to.name ?? ''),
    hasToken: session.isAuthenticated(),
    fullPath: to.fullPath,
  })
  if (redirect) {
    return { name: redirect.name, query: redirect.query }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title
  document.title = title ? `${String(title)} · 律师 Agent` : '律师 Agent'
})

export default router
