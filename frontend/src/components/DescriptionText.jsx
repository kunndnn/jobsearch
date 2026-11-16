import { Fragment } from 'react'

// Minimal markdown-ish renderer for scraped descriptions. Everything becomes React text
// nodes (never innerHTML); only http(s) links are turned into anchors.

const INLINE = /(\*\*[^*\n]+?\*\*|__[^_\n]+?__|\[[^\]\n]+\]\([^)\s]+\))/g
const LINK = /^\[([^\]]+)\]\(([^)\s]+)\)$/

const unescapeMd = (text) => text.replace(/\\([\\`*_{}[\]()#+\-.!|>~])/g, '$1')

function renderInline(text) {
  return text.split(INLINE).map((part, i) => {
    if (!part) return null
    if ((part.startsWith('**') && part.endsWith('**')) || (part.startsWith('__') && part.endsWith('__'))) {
      return <strong key={i}>{unescapeMd(part.slice(2, -2))}</strong>
    }
    const link = part.match(LINK)
    if (link) {
      const label = unescapeMd(link[1])
      return /^https?:\/\//i.test(link[2]) ? (
        <a key={i} href={link[2]} target="_blank" rel="noopener noreferrer">
          {label}
        </a>
      ) : (
        label
      )
    }
    return unescapeMd(part)
  })
}

function parseBlocks(text) {
  const blocks = []
  let paragraph = []
  let list = null

  const flush = () => {
    if (paragraph.length) blocks.push({ type: 'p', lines: paragraph })
    if (list) blocks.push(list)
    paragraph = []
    list = null
  }

  for (const rawLine of text.replace(/\r\n?/g, '\n').split('\n')) {
    const line = rawLine.trim()
    const heading = line.match(/^#{1,6}\s+(.*)$/)
    const bullet = line.match(/^(?:[-*+•▪●]|\\[-*+])\s+(.*)$/)
    const numbered = line.match(/^(\d+)[.)]\s+(.*)$/)

    if (!line) {
      flush()
    } else if (heading) {
      flush()
      blocks.push({ type: 'h', text: heading[1] })
    } else if (bullet || numbered) {
      const ordered = Boolean(numbered)
      if (paragraph.length || (list && list.ordered !== ordered)) flush()
      list = list || { type: 'list', ordered, items: [] }
      list.items.push(bullet ? bullet[1] : numbered[2])
    } else {
      if (list) flush()
      paragraph.push(line)
    }
  }
  flush()
  return blocks
}

function renderParagraph(lines) {
  return lines.map((line, i) => (
    <Fragment key={i}>
      {i > 0 && <br />}
      {renderInline(line)}
    </Fragment>
  ))
}

export default function DescriptionText({ text }) {
  if (!text || !text.trim()) {
    return <p className="muted">No description was captured for this job. Open the posting to read it.</p>
  }
  return (
    <div className="description-text">
      {parseBlocks(text).map((block, i) => {
        if (block.type === 'h') return <h5 key={i}>{renderInline(block.text)}</h5>
        if (block.type === 'p') return <p key={i}>{renderParagraph(block.lines)}</p>
        const ListTag = block.ordered ? 'ol' : 'ul'
        return (
          <ListTag key={i}>
            {block.items.map((item, j) => (
              <li key={j}>{renderInline(item)}</li>
            ))}
          </ListTag>
        )
      })}
    </div>
  )
}
