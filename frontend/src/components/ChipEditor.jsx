import { useId, useState } from 'react'
import { Plus, X } from 'lucide-react'

function addValues(values, raw) {
  const next = [...values]
  const seen = new Set(values.map((v) => v.toLowerCase()))
  for (const item of raw) {
    const value = item.trim()
    if (value && !seen.has(value.toLowerCase())) {
      seen.add(value.toLowerCase())
      next.push(value)
    }
  }
  return next
}

/**
 * Editable list of strings. Enter / blur adds (comma too when `commaSeparates`),
 * Backspace on an empty input removes the last chip; duplicates are ignored case-insensitively.
 */
export default function ChipEditor({
  label,
  hint,
  values,
  onChange,
  placeholder = 'Type and press Enter',
  commaSeparates = true,
  variant = '',
}) {
  const [input, setInput] = useState('')
  const inputId = useId()
  const list = values || []

  const commit = (raw = [input]) => {
    const next = addValues(list, raw)
    if (next.length !== list.length) onChange(next)
    setInput('')
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' || (commaSeparates && e.key === ',')) {
      e.preventDefault()
      commit()
    } else if (e.key === 'Backspace' && !input && list.length) {
      onChange(list.slice(0, -1))
    }
  }

  const handlePaste = (e) => {
    const text = e.clipboardData.getData('text')
    const parts = text.split(commaSeparates ? /[\n,]/ : /\n/)
    if (parts.length > 1) {
      e.preventDefault()
      const { selectionStart, selectionEnd } = e.target
      parts[0] = input.slice(0, selectionStart) + parts[0]
      parts[parts.length - 1] += input.slice(selectionEnd)
      commit(parts)
    }
  }

  return (
    <div className="field">
      {label && (
        <label className="field-label" htmlFor={inputId}>
          {label}
          <span className="field-count">{list.length}</span>
        </label>
      )}
      <div className={`chip-editor ${variant}`}>
        {list.map((value) => (
          <span key={value} className="chip">
            <span>{value}</span>
            <button
              type="button"
              className="chip-remove"
              onClick={() => onChange(list.filter((v) => v !== value))}
              aria-label={`Remove ${value}`}
            >
              <X size={12} />
            </button>
          </span>
        ))}
        <input
          id={inputId}
          className="chip-input"
          value={input}
          placeholder={list.length ? '' : placeholder}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onBlur={() => input.trim() && commit()}
        />
        {input.trim() && (
          <button type="button" className="chip-add" onClick={() => commit()} aria-label="Add">
            <Plus size={14} />
          </button>
        )}
      </div>
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  )
}
