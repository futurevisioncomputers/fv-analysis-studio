// Render a report HTML to PDF exactly the way the operator does: open it and
// print. Used to verify the printed artefact, not the screen one — the two
// disagreed badly enough once to ship a 97-page file of empty frames.
import { chromium } from '@playwright/test'
import { pathToFileURL } from 'node:url'

const [, , src, out] = process.argv
const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto(pathToFileURL(src).href, { waitUntil: 'networkidle' })
await page.emulateMedia({ media: 'print' })
await page.pdf({
  path: out,
  format: 'A4',
  printBackground: true,
  margin: { top: '12mm', bottom: '12mm', left: '10mm', right: '10mm' },
})
await browser.close()
console.log('pdf written:', out)
