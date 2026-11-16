import { useId } from 'react'
import { Save, Undo2 } from 'lucide-react'

export function Toggle({ checked, onChange, label, hint, disabled }) {
  return (
    <label className={`toggle ${disabled ? 'disabled' : ''}`}>
      <input type="checkbox" checked={Boolean(checked)} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle-track" aria-hidden="true">
        <span className="toggle-thumb" />
      </span>
      <span className="toggle-text">
        <span>{label}</span>
        {hint && <span className="field-hint">{hint}</span>}
      </span>
    </label>
  )
}

/** Number input that keeps '' while the field is being cleared, so validation can flag it. */
export function NumberField({ label, value, onChange, min, max, step = 1, hint, suffix }) {
  const id = useId()
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <div className="input-with-suffix">
        <input
          id={id}
          className="input"
          type="number"
          inputMode="decimal"
          value={value ?? ''}
          min={min}
          max={max}
          step={step}
          onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
        />
        {suffix && <span className="input-suffix">{suffix}</span>}
      </div>
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  )
}

export function TextField({ label, value, onChange, placeholder, hint, required, type = 'text' }) {
  const id = useId()
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
        {required && <span className="required">*</span>}
      </label>
      <input
        id={id}
        className="input"
        type={type}
        value={value ?? ''}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  )
}

/** Sticky footer: dirty indicator, client validation (blocks save), server 422 details, Discard and Save. */
export function SaveBar({ dirty, saving, errors = [], serverErrors = [], onSave, onDiscard, saveLabel = 'Save changes' }) {
  const blocked = errors.length > 0
  return (
    <div className={`save-bar ${dirty ? 'dirty' : ''}`}>
      <div className="save-bar-status">
        <span className={`dirty-dot ${dirty ? 'on' : ''}`} />
        <span>{dirty ? 'Unsaved changes' : 'All changes saved'}</span>
      </div>
      {(blocked || serverErrors.length > 0) && (
        <ul className="form-errors">
          {errors.map((msg) => (
            <li key={`c-${msg}`}>{msg}</li>
          ))}
          {serverErrors.map((msg) => (
            <li key={`s-${msg}`}>Server: {msg}</li>
          ))}
        </ul>
      )}
      <div className="save-bar-actions">
        <button type="button" className="pill-btn" onClick={onDiscard} disabled={!dirty || saving}>
          <Undo2 size={14} /> Discard
        </button>
        <button type="button" className="btn-primary" onClick={onSave} disabled={!dirty || saving || blocked}>
          <Save size={15} /> {saving ? 'Saving…' : saveLabel}
        </button>
      </div>
    </div>
  )
}
