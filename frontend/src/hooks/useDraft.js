import { useState } from 'react'

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

/**
 * Editable copy of server data. When `source` changes (refetch/save) the draft follows it,
 * unless the user has unsaved edits. `reset(next)` replaces both (after save / discard).
 */
export function useDraft(source, toDraft) {
  const [base, setBase] = useState(source)
  const [draft, setDraft] = useState(() => (source ? toDraft(source) : null))

  if (source !== base) {
    const hadEdits = base && draft && !same(draft, toDraft(base))
    setBase(source)
    if (!hadEdits) setDraft(source ? toDraft(source) : null)
  }

  const dirty = Boolean(source && draft) && !same(draft, toDraft(source))

  const reset = (next) => {
    setBase(next)
    setDraft(next ? toDraft(next) : null)
  }

  return { draft, setDraft, dirty, reset }
}
