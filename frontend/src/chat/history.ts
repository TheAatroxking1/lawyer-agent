import { ref } from 'vue'

// A notification only: private history is loaded with server authorization.
export const historyRevision = ref(0)
export function notifyHistoryChanged(): void { historyRevision.value += 1 }
