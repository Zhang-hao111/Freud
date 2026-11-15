import { Fragment, lazy, Suspense, useEffect, useId, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { Definition, Nodes, Parent } from 'mdast'
import { fromMarkdown } from 'mdast-util-from-markdown'
import { gfmFromMarkdown } from 'mdast-util-gfm'
import { mathFromMarkdown } from 'mdast-util-math'
import { gfm } from 'micromark-extension-gfm'
import { math } from 'micromark-extension-llm-math'
import Prism from 'prismjs'
import 'prismjs/components/prism-bash.js'
import 'prismjs/components/prism-json.js'
import 'prismjs/components/prism-python.js'
import 'prismjs/components/prism-typescript.js'
import { cjkFriendlyStrong } from '../dsh/cjkFriendlyStrong.js'
import './markdown.css'

const artifactPath = /^\/api\/workflows\/[a-z0-9-]+\/[a-z][a-z0-9-]*\/[a-f0-9]{32}(?:\/[a-z_]+)?$/
const plotPath = /^\/api\/workflows\/[a-z0-9-]+\/plot\/[a-f0-9]{32}\/[a-z_]+$/
const MathMarkup = lazy(() => import('./MathMarkup.js'))

function safeLink(url: string): string | undefined {
  if (artifactPath.test(url)) return url
  try {
    const parsed = new URL(url)
    if (parsed.protocol === 'https:' || parsed.protocol === 'http:') return parsed.href
  } catch {
    return undefined
  }
  return undefined
}

function Diagram({ source }: { source: string }) {
  const id = useId().replace(/[^a-z0-9-]/gi, '')
  const [drawing, setDrawing] = useState<{ source: string; url: string } | null>(null)

  useEffect(() => {
    let current = true
    void import('mermaid').then(async ({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' })
      const { svg } = await mermaid.render(`freud-diagram-${id}`, source)
      if (current) setDrawing({ source, url: 'data:image/svg+xml,' + encodeURIComponent(svg) })
    }).catch(() => undefined)
    return () => { current = false }
  }, [id, source])

  return drawing?.source === source ? <img src={drawing.url} alt="Mermaid 图示" /> : null
}

function Code({ value, language }: { value: string; language: string }) {
  const [copied, setCopied] = useState(false)
  const grammar = Object.hasOwn(Prism.languages, language) ? Prism.languages[language] : undefined
  const highlighted = grammar && typeof grammar === 'object'
    ? Prism.highlight(value, grammar, language)
    : undefined
  return <div className="markdown-code">
    {language === 'mermaid' && <Diagram source={value} />}
    <div className="markdown-code-header">
      {language || '代码'}
      <button type="button" onClick={() => void navigator.clipboard?.writeText(value).then(
        () => setCopied(true), () => setCopied(false),
      )}>{copied ? '已复制' : '复制'}</button>
    </div>
    <pre>{highlighted
      ? <code dangerouslySetInnerHTML={{ __html: highlighted }} />
      : <code>{value}</code>}</pre>
  </div>
}

function Picture({ url, alt }: { url: string; alt?: string | null }) {
  if (plotPath.test(url)) return <img src={url} alt={alt || '分析图表'} loading="lazy" />
  const href = safeLink(url)
  return href ? <a href={href} target="_blank" rel="noreferrer">{alt || href}</a> : alt
}

function Anchor({ url, children }: { url?: string; children: ReactNode }) {
  const href = url && safeLink(url)
  return href ? <a href={href} target="_blank" rel="noreferrer">{children}</a> : children
}

function renderMarkdown(root: Nodes) {
  const definitions = new Map<string, Definition>()
  function collect(node: Nodes) {
    if (node.type === 'definition') definitions.set(node.identifier, node)
    if ('children' in node) node.children.forEach(collect)
  }
  collect(root)

  function children(node: Parent) {
    return node.children.map((child, index) => <Fragment key={index}>{render(child)}</Fragment>)
  }

  function render(node: Nodes): ReactNode {
    switch (node.type) {
      case 'root':
      case 'tableCell':
        return children(node)
      case 'paragraph':
        return <p>{children(node)}</p>
      case 'heading': {
        const Heading = `h${node.depth}` as const
        return <Heading>{children(node)}</Heading>
      }
      case 'text':
        return node.value
      case 'emphasis':
        return <em>{children(node)}</em>
      case 'strong':
        return <strong>{children(node)}</strong>
      case 'delete':
        return <del>{children(node)}</del>
      case 'blockquote':
        return <blockquote>{children(node)}</blockquote>
      case 'break':
        return <br />
      case 'thematicBreak':
        return <hr />
      case 'list': {
        const List = node.ordered ? 'ol' : 'ul'
        return <List start={node.start ?? undefined}>{children(node)}</List>
      }
      case 'listItem':
        return <li>{node.checked != null && <input type="checkbox" checked={node.checked} disabled />}{children(node)}</li>
      case 'inlineCode':
        return <code>{node.value}</code>
      case 'code':
        return <Code value={node.value} language={node.lang?.toLowerCase() ?? ''} />
      case 'link':
        return <Anchor url={node.url}>{children(node)}</Anchor>
      case 'linkReference':
        return <Anchor url={definitions.get(node.identifier)?.url}>{children(node)}</Anchor>
      case 'image':
        return <Picture url={node.url} alt={node.alt} />
      case 'imageReference':
        return <Picture url={definitions.get(node.identifier)?.url ?? ''} alt={node.alt} />
      case 'table': {
        const [header, ...rows] = node.children
        return <div className="markdown-table-wrapper"><table>
          <thead><tr>{header?.children.map((cell, index) => <th key={index}>{children(cell)}</th>)}</tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>{row.children.map(
            (cell, column) => <td key={column}>{children(cell)}</td>,
          )}</tr>)}</tbody>
        </table></div>
      }
      case 'inlineMath':
      case 'math':
        return <Suspense fallback={<code>{node.value}</code>}>
          <MathMarkup value={node.value} display={node.type === 'math'} />
        </Suspense>
      case 'html':
        if (/^<br\s*\/?>$/i.test(node.value)) return <br />
        return node.value.trimStart().startsWith('<!--') ? null : node.value
      default:
        return null
    }
  }

  return render(root)
}

export function Markdown({ content }: { content: string }) {
  const rendered = useMemo(() => renderMarkdown(fromMarkdown(content, {
    extensions: [gfm({ singleTilde: false }), cjkFriendlyStrong(), math()],
    mdastExtensions: [gfmFromMarkdown(), mathFromMarkdown()],
  })), [content])

  return <div className="ds-markdown">{rendered}</div>
}
