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
        // Landing: the conversation page is the product surface; logging in
        // happens only when the visitor actually talks to the AI.
        { path: '', redirect: { name: 'chat' } },
        {
          path: 'home',
          name: 'home',
          component: () => import('../views/HomeView.vue'),
          meta: { title: '首页' },
        },
        {
          path: 'chat',
          name: 'chat',
          component: () => import('../views/ChatView.vue'),
          meta: { title: '法规对话' },
        },
        {
          path: 'ask',
          name: 'ask',
          component: () => import('../views/AskView.vue'),
          meta: { title: '法规问答' },
        },
        {
          path: 'corpus',
          name: 'corpus',
          component: () => import('../views/CorpusView.vue'),
          meta: { title: '法规语料' },
        },
        {
          path: 'corpus/:instrumentId',
          name: 'corpus-instrument',
          component: () => import('../views/CorpusInstrumentView.vue'),
          meta: { title: '法规详情' },
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
