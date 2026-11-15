import katex from 'katex'
import 'katex/dist/katex.min.css'

export default function MathMarkup({ value, display }: { value: string; display: boolean }) {
  const html = katex.renderToString(value, { displayMode: display, throwOnError: false, trust: false })
  return <span className="markdown-math" dangerouslySetInnerHTML={{ __html: html }} />
}
