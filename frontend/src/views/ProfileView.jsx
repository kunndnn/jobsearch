import { useState } from 'react'
import { ArrowDown, ArrowUp, Ban, Layers, MapPin, Plus, Target, Trash2, User } from 'lucide-react'
import ChipEditor from '../components/ChipEditor.jsx'
import { NumberField, SaveBar, TextField } from '../components/FormControls.jsx'
import { useDraft } from '../hooks/useDraft.js'
import { detailLines, saveProfile } from '../api.js'
import { TRACK_PRIORITIES, WEIGHTED_SKILL_CATEGORIES } from '../utils/labels.js'

const RESERVED_TRACK_IDS = ['all', 'other']
const TRACK_ID_PATTERN = /^[A-Za-z0-9_-]+$/

const TEXT_FIELDS = [
  ['name', 'Name', true],
  ['headline', 'Headline', false, 'e.g. SDE-1 · Backend & GenAI · 1-3 YoE'],
  ['email', 'Email', false],
  ['phone', 'Phone', false],
  ['experience_bracket', 'Experience bracket', false, 'e.g. 1-3 years'],
  ['education', 'Education', false],
]

const toDraft = (p) => ({
  name: p.name ?? '',
  headline: p.headline ?? '',
  email: p.email ?? '',
  phone: p.phone ?? '',
  experience_bracket: p.experience_bracket ?? '',
  education: p.education ?? '',
  experience_years: p.experience_years ?? '',
  max_experience_years: p.max_experience_years ?? '',
  preferred_locations: p.preferred_locations ?? [],
  skills: Object.entries(p.skills || {}).map(([key, items]) => ({ key, items: items || [] })),
  target_tracks: (p.target_tracks || []).map((t) => ({
    ...t,
    id: t.id ?? '',
    title: t.title ?? '',
    keywords: t.keywords ?? [],
    title_tokens: t.title_tokens ?? [],
    priority: t.priority ?? 'high',
  })),
  disqualifiers: p.disqualifiers ?? [],
})

function fromDraft(draft, original) {
  const trimmed = Object.fromEntries(TEXT_FIELDS.map(([key]) => [key, draft[key].trim()]))
  return {
    ...original,
    ...trimmed,
    experience_years: Number(draft.experience_years),
    max_experience_years: Number(draft.max_experience_years),
    preferred_locations: draft.preferred_locations,
    skills: Object.fromEntries(draft.skills.map((row) => [row.key.trim(), row.items])),
    // Empty title tokens -> null so the server derives them (template defaults, else keywords).
    target_tracks: draft.target_tracks.map((t) => ({
      ...t,
      id: t.id.trim(),
      title: t.title.trim(),
      title_tokens: t.title_tokens.length ? t.title_tokens : null,
    })),
    disqualifiers: draft.disqualifiers,
  }
}

function findDuplicates(values) {
  const seen = new Set()
  const dupes = new Set()
  for (const v of values) (seen.has(v) ? dupes : seen).add(v)
  return [...dupes]
}

function validate(draft) {
  const errors = []
  if (!draft.name.trim()) errors.push('Name is required.')
  const years = draft.experience_years
  if (typeof years !== 'number' || !Number.isFinite(years) || years < 0 || years > 60) {
    errors.push('Experience (years) must be a number from 0 to 60.')
  }
  const senior = draft.max_experience_years
  if (typeof senior !== 'number' || !Number.isFinite(senior) || senior <= 0 || senior > 60) {
    errors.push('Senior threshold must be greater than 0 and at most 60.')
  }
  const categories = draft.skills.map((r) => r.key.trim())
  if (categories.some((k) => !k)) errors.push('Every skill category needs a name.')
  for (const dup of findDuplicates(categories.filter(Boolean))) errors.push(`Skill category “${dup}” is used twice.`)
  const ids = draft.target_tracks.map((t) => t.id.trim())
  if (ids.some((id) => !id)) errors.push('Every target track needs an id.')
  for (const dup of findDuplicates(ids.filter(Boolean))) errors.push(`Track id “${dup}” is used twice.`)
  for (const t of draft.target_tracks) {
    if (t.id.trim() && !t.title_tokens.length && !t.keywords.length) {
      errors.push(`Track “${t.id.trim()}” needs title tokens or keywords, or it can never match a job.`)
    }
  }
  for (const id of ids.filter(Boolean)) {
    if (RESERVED_TRACK_IDS.includes(id.toLowerCase())) errors.push(`Track id “${id}” is reserved; pick another.`)
    else if (!TRACK_ID_PATTERN.test(id)) errors.push(`Track id “${id}” may only use letters, digits, “_” and “-”.`)
  }
  return errors
}

function uniqueName(base, taken) {
  let name = base
  for (let i = 2; taken.includes(name); i++) name = `${base}_${i}`
  return name
}

const replaceAt = (list, index, value) => list.map((item, i) => (i === index ? value : item))

function SkillsSection({ skills, onChange }) {
  const update = (index, patch) => onChange(replaceAt(skills, index, { ...skills[index], ...patch }))
  const add = () =>
    onChange([...skills, { key: uniqueName('new_category', skills.map((r) => r.key)), items: [] }])

  return (
    <div className="panel">
      <div className="panel-header">
        <h3 className="panel-title">
          <Layers size={18} /> Skills
        </h3>
        <button type="button" className="pill-btn" onClick={add}>
          <Plus size={14} /> Add category
        </button>
      </div>
      <p className="field-hint">
        {WEIGHTED_SKILL_CATEGORIES.join(', ')} carry their own scoring weights; any other category counts as a generic
        skill with a small weight.
      </p>
      <div className="stack">
        {skills.map((row, index) => (
          <div className="sub-card" key={index}>
            <div className="sub-card-head">
              <input
                className="input category-input"
                value={row.key}
                aria-label="Category name"
                onChange={(e) => update(index, { key: e.target.value })}
              />
              <span className={`weight-tag ${WEIGHTED_SKILL_CATEGORIES.includes(row.key.trim()) ? 'weighted' : ''}`}>
                {WEIGHTED_SKILL_CATEGORIES.includes(row.key.trim()) ? 'weighted' : 'generic'}
              </span>
              <button
                type="button"
                className="btn-icon"
                title="Remove category"
                onClick={() => onChange(skills.filter((_, i) => i !== index))}
              >
                <Trash2 size={15} />
              </button>
            </div>
            <ChipEditor values={row.items} onChange={(items) => update(index, { items })} placeholder="Add a skill" />
          </div>
        ))}
      </div>
    </div>
  )
}

function TracksSection({ tracks, onChange }) {
  const update = (index, patch) => onChange(replaceAt(tracks, index, { ...tracks[index], ...patch }))
  const move = (index, delta) => {
    const next = [...tracks]
    const [item] = next.splice(index, 1)
    next.splice(index + delta, 0, item)
    onChange(next)
  }
  const add = () =>
    onChange([
      ...tracks,
      {
        id: uniqueName('new_track', tracks.map((t) => t.id)),
        title: '',
        keywords: [],
        title_tokens: [],
        priority: 'high',
      },
    ])

  return (
    <div className="panel">
      <div className="panel-header">
        <h3 className="panel-title">
          <Target size={18} /> Target tracks
        </h3>
        <button type="button" className="pill-btn" onClick={add}>
          <Plus size={14} /> Add track
        </button>
      </div>
      <p className="field-hint">
        A job gets the first track (top to bottom) whose title tokens appear in its title; otherwise “other”. Tracks
        also become the role filters on the Jobs tab.
      </p>
      <div className="stack">
        {tracks.map((track, index) => (
          <div className="sub-card" key={index}>
            <div className="sub-card-head">
              <span className="track-order">{index + 1}</span>
              <div className="field-grid compact grow">
                <TextField
                  label="Id"
                  value={track.id}
                  onChange={(id) => update(index, { id })}
                  placeholder="e.g. backend_heavy"
                  required
                />
                <TextField label="Title" value={track.title} onChange={(title) => update(index, { title })} />
                <label className="field">
                  <span className="field-label">Priority</span>
                  <select
                    className="input"
                    value={track.priority}
                    onChange={(e) => update(index, { priority: e.target.value })}
                  >
                    {[...new Set([...TRACK_PRIORITIES, track.priority])].map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="track-actions">
                <button type="button" className="btn-icon" title="Move up" disabled={index === 0} onClick={() => move(index, -1)}>
                  <ArrowUp size={15} />
                </button>
                <button
                  type="button"
                  className="btn-icon"
                  title="Move down"
                  disabled={index === tracks.length - 1}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDown size={15} />
                </button>
                <button
                  type="button"
                  className="btn-icon"
                  title="Remove track"
                  onClick={() => onChange(tracks.filter((_, i) => i !== index))}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
            <ChipEditor
              label="Title tokens"
              values={track.title_tokens}
              onChange={(title_tokens) => update(index, { title_tokens })}
              placeholder="e.g. backend"
              hint="Matched against job titles on word boundaries. Leave empty to fill them on save from the keywords (or the built-in tokens of a template track)."
            />
            <ChipEditor
              label="Keywords"
              values={track.keywords}
              onChange={(keywords) => update(index, { keywords })}
              placeholder="e.g. Backend Engineer"
            />
          </div>
        ))}
      </div>
    </div>
  )
}

export default function ProfileView({ profile, profileError, onProfileSaved, notify }) {
  const { draft, setDraft, dirty, reset } = useDraft(profile, toDraft)
  const [saving, setSaving] = useState(false)
  const [serverErrors, setServerErrors] = useState([])

  if (!draft) {
    return (
      <div className="loading-block">
        {profileError ? (
          <p className="form-error">Could not load your profile: {profileError.message}</p>
        ) : (
          <>
            <div className="spinner large" />
            <p>Loading your profile…</p>
          </>
        )}
      </div>
    )
  }

  const errors = validate(draft)
  const set = (patch) => setDraft((d) => ({ ...d, ...patch }))

  const save = async () => {
    setSaving(true)
    setServerErrors([])
    try {
      const res = await saveProfile(fromDraft(draft, profile))
      reset(res.profile)
      onProfileSaved(res)
    } catch (err) {
      setServerErrors(err.status === 422 ? detailLines(err.detail) : [err.message])
      notify({ type: 'error', title: 'Profile not saved', message: err.message })
    } finally {
      setSaving(false)
    }
  }

  const discard = () => {
    reset(profile)
    setServerErrors([])
  }

  return (
    <div className="profile">
      <div className="panel">
        <div className="panel-header">
          <h3 className="panel-title">
            <User size={18} /> About you
          </h3>
          <span className="muted small">Used for the header and for scoring every job</span>
        </div>
        <div className="field-grid">
          {TEXT_FIELDS.map(([key, label, required, placeholder]) => (
            <TextField
              key={key}
              label={label}
              value={draft[key]}
              required={required}
              placeholder={placeholder}
              type={key === 'email' ? 'email' : key === 'phone' ? 'tel' : 'text'}
              onChange={(value) => set({ [key]: value })}
            />
          ))}
          <NumberField
            label="Experience (years)"
            value={draft.experience_years}
            min={0}
            step={0.1}
            onChange={(experience_years) => set({ experience_years })}
          />
          <NumberField
            label="Senior threshold (years)"
            value={draft.max_experience_years}
            min={0}
            step={0.5}
            hint="Jobs needing at least this much experience are marked senior."
            onChange={(max_experience_years) => set({ max_experience_years })}
          />
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <h3 className="panel-title">
            <MapPin size={18} /> Preferred locations
          </h3>
        </div>
        <ChipEditor
          values={draft.preferred_locations}
          onChange={(preferred_locations) => set({ preferred_locations })}
          placeholder="e.g. Bengaluru"
          commaSeparates={false}
          hint="Shown as location filters on the Jobs tab."
        />
      </div>

      <SkillsSection skills={draft.skills} onChange={(skills) => set({ skills })} />

      <TracksSection tracks={draft.target_tracks} onChange={(target_tracks) => set({ target_tracks })} />

      <div className="panel">
        <div className="panel-header">
          <h3 className="panel-title">
            <Ban size={18} /> Disqualifiers
          </h3>
        </div>
        <ChipEditor
          values={draft.disqualifiers}
          onChange={(disqualifiers) => set({ disqualifiers })}
          placeholder="e.g. PHP"
          variant="negative"
          hint="Each one found in a job lowers its score by 25 (unless Node.js/TypeScript also appear)."
        />
      </div>

      <SaveBar
        dirty={dirty}
        saving={saving}
        errors={dirty ? errors : []}
        serverErrors={serverErrors}
        onSave={save}
        onDiscard={discard}
        saveLabel="Save & re-score"
      />
    </div>
  )
}
