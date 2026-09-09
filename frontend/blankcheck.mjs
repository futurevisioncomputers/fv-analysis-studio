// Which printed pages carry no ink?
//
// "Blank page" is a property of the printed artefact, not the DOM, so this
// measures the way the paper does: lay the document out under print media,
// slice it into A4-sized bands, and ask which bands contain no text and no
// drawn graphic. A band that is merely short (a section ending early) is not
// blank; a band with nothing at all is.
import { chromium } from '@playwright/test'
import { pathToFileURL } from 'node:url'

const [, , src] = process.argv
const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto(pathToFileURL(src).href, { waitUntil: 'networkidle' })
await page.emulateMedia({ media: 'print' })
// A4 at 96dpi, less the 12mm/10mm margins the PDF is printed with.
await page.setViewportSize({ width: 718, height: 1033 })

const result = await page.evaluate(() => {
  const PAGE_H = 1033
  const total = document.documentElement.scrollHeight
  const pages = Math.ceil(total / PAGE_H)

  // Every element that actually puts ink on paper.
  const inked = []
  for (const el of document.querySelectorAll('*')) {
    const style = getComputedStyle(el)
    if (style.display === 'none' || style.visibility === 'hidden') continue
    const r = el.getBoundingClientRect()
    if (r.height <= 0 || r.width <= 0) continue
    const tag = el.tagName.toLowerCase()
    const isGraphic = tag === 'svg' || tag === 'rect' || tag === 'path' || tag === 'line'
    const ownText = [...el.childNodes]
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim())
      .join('')
    if (!isGraphic && !ownText) continue
    const top = r.top + window.scrollY
    inked.push({ top, bottom: top + r.height, kind: isGraphic ? 'g' : 't' })
  }

  const report = []
  for (let i = 0; i < pages; i++) {
    const a = i * PAGE_H
    const b = a + PAGE_H
    const hits = inked.filter((x) => x.bottom > a + 2 && x.top < b - 2)
    report.push({
      page: i + 1,
      text: hits.filter((h) => h.kind === 't').length,
      graphics: hits.filter((h) => h.kind === 'g').length,
    })
  }
  return { totalHeight: total, pages, report }
})

console.log(`document height ${result.totalHeight}px -> ${result.pages} A4 pages`)
const blank = result.report.filter((r) => r.text === 0 && r.graphics === 0)
for (const r of result.report) {
  const flag = r.text === 0 && r.graphics === 0 ? '  <-- BLANK' : ''
  console.log(`  page ${String(r.page).padStart(2)}: text=${String(r.text).padStart(3)} graphics=${String(r.graphics).padStart(3)}${flag}`)
}
console.log(blank.length ? `BLANK PAGES: ${blank.map((b) => b.page).join(', ')}` : 'NO BLANK PAGES')
await browser.close()
