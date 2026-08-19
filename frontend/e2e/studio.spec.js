import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))

// The real institute workbook. A synthetic fixture that joins perfectly would
// not have caught the float-vs-int bug that reported a 99% join as 0%.
//
// Resolved from the sibling plugin checkout — the same assumption the backend
// already makes to import `agents` — so the suite is not tied to one machine's
// Downloads folder. FV_TEST_WORKBOOK overrides it.
const WORKBOOK = process.env.FV_TEST_WORKBOOK ?? path.resolve(
  HERE, '../../../fv-analysis-marketplace-main/output/FV_Students_v2.xlsx')

const QUESTION = 'Which branches and course categories drive revenue and completion?'

const hasWorkbook = fs.existsSync(WORKBOOK)
test.skip(!hasWorkbook, `workbook not found at ${WORKBOOK}`)

/** Upload the workbook, optionally with a question typed first. */
async function upload(page, question) {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Upload a workbook' })).toBeVisible()
  if (question) {
    await page.getByPlaceholder(/Which branches/).fill(question)
  }
  await page.locator('input[type=file]').setInputFiles(WORKBOOK)
  await expect(page.getByRole('heading', { name: 'Dataset discovery' }))
    .toBeVisible({ timeout: 60_000 })
}

test.describe('FV Analysis Studio', () => {
  test('the upload screen asks the question before taking the file', async ({ page }) => {
    await page.goto('/')
    // Ordering is the fix for a real bug: the question box used to sit BELOW
    // the drop zone, so dropping a file first sent an empty question and the
    // run stopped at stage 1 asking for one.
    const question = page.getByPlaceholder(/Which branches/)
    const drop = page.locator('.drop')
    const qBox = await question.boundingBox()
    const dBox = await drop.boundingBox()
    expect(qBox.y).toBeLessThan(dBox.y)

    // The whole pipeline is advertised before anything runs.
    await expect(page.getByRole('heading', { name: 'What will run' })).toBeVisible()
    await expect(page.locator('.chip')).toHaveCount(12)
  })

  test('discovery reports the joins before any stage runs', async ({ page }) => {
    await upload(page, QUESTION)

    await expect(page.locator('.banner.ok')).toContainText('every foreign key resolves')

    const sheets = page.locator('table').first()
    await expect(sheets).toContainText('students')
    await expect(sheets).toContainText('fee_receipts')
    await expect(sheets).toContainText('certificates')
    // A reference tab feeding dropdowns is not a data source.
    await expect(sheets).not.toContainText('lookups')

    // Every foreign key resolves completely on this workbook.
    const links = page.locator('table').nth(1)
    await expect(links).toContainText('fee_receipts → students')
    await expect(links.locator('tr')).toContainText([/100%/, /100%/])
  })

  test('a run without a question blocks and is answerable in place', async ({ page }) => {
    await upload(page)                       // deliberately no question
    await page.getByRole('button', { name: 'Run everything' }).click()

    // Problem Definition refuses to invent a goal. The blocked stage selects
    // itself, because it is the one thing the operator must act on.
    const banner = page.locator('.banner.bad')
    await expect(banner).toContainText(/clarification/i, { timeout: 60_000 })

    // Answering must not require re-uploading a file that has not changed.
    // `exact` matters: Playwright matches an accessible name as a SUBSTRING by
    // default, so a bare 'Run' also picks up 'Run everything'.
    await page.getByPlaceholder(/Which branches/).fill(QUESTION)
    await page.getByRole('button', { name: 'Run', exact: true }).click()

    await expect(page.locator('.pill.done').first()).toBeVisible({ timeout: 90_000 })
    await expect(banner).toBeHidden({ timeout: 90_000 })
  })

  test('answering before the module list arrives still runs', async ({ page }) => {
    // The bug this pins: `modules` starts empty and fills from a fetch, and
    // the answer sent an EXPLICIT scope of no modules if you got there first —
    // 400 "at least one module has to be in scope", with the chips it named
    // rendering underneath the error a moment later. Only reachable by typing
    // fast, so the ordinary test passed on a quick machine and this one holds
    // the fetch open to make it deterministic.
    await page.route('**/api/modules', async (route) => {
      await new Promise((r) => setTimeout(r, 5_000))
      await route.continue()
    })

    await upload(page)
    await page.getByRole('button', { name: 'Run everything' }).click()
    await expect(page.locator('.banner.bad')).toContainText(/clarification/i,
                                                            { timeout: 60_000 })

    await page.getByPlaceholder(/Which branches/).fill(QUESTION)
    await page.getByRole('button', { name: 'Run', exact: true }).click()

    await expect(page.locator('.banner.bad')).not.toContainText(
      /at least one module/i, { timeout: 30_000 })
    await expect(page.locator('.pill.done').first()).toBeVisible({ timeout: 90_000 })
  })

  test('the rail runs every agent and the report opens', async ({ page }) => {
    await upload(page, QUESTION)
    await page.getByRole('button', { name: 'Run everything' }).click()

    // Exactly one agent works at a time; motion means working.
    await expect(page.locator('.pill.running')).toHaveCount(1, { timeout: 30_000 })

    // Report Writer is last; when it is done the whole rail is done.
    const rail = page.locator('.rail')
    await expect(rail.getByText('Report Writer')).toBeVisible()
    await expect(page.locator('.node[data-status="done"]'))
      .toHaveCount(10, { timeout: 120_000 })
    await expect(page.locator('.pill.running')).toHaveCount(0)

    // Timeline shows where the time went.
    await expect(page.getByRole('heading', { name: 'Where the time went' })).toBeVisible()

    // The tab, not the 'Report Writer' node in the rail — substring matching
    // resolves a bare 'Report' to both.
    await page.getByRole('button', { name: 'Report', exact: true }).click()
    const frame = page.frameLocator('iframe.report')
    await expect(frame.locator('body')).toContainText(/./, { timeout: 30_000 })
  })

  test('an agent shows its work report and its performance report', async ({ page }) => {
    await upload(page, QUESTION)
    await page.getByRole('button', { name: 'Run everything' }).click()
    await expect(page.locator('.node[data-status="done"]'))
      .toHaveCount(10, { timeout: 120_000 })

    await page.locator('.node', { hasText: 'Data Engineer' }).click()
    await expect(page.getByRole('heading', { name: /Data Engineer/ })).toBeVisible()

    // KPI tiles: the two or three numbers that describe this agent's work.
    await expect(page.locator('.tile')).not.toHaveCount(0)
    await expect(page.locator('.tiles')).toContainText('of run time')

    // Work report — the agent's own details, not the studio's.
    await expect(page.locator('.list li').first()).toBeVisible()

    // Performance report.
    await page.getByRole('button', { name: /Performance/ }).click()
    await expect(page.locator('.kv')).toContainText('duration')
    await expect(page.locator('.kv')).toContainText('rows out')

    await page.getByRole('button', { name: /Artifacts/ }).click()
    await expect(page.locator('.chip')).not.toHaveCount(0)
  })

  test('the data model marks what this upload cannot fill', async ({ page }) => {
    await upload(page, QUESTION)
    await page.getByRole('button', { name: 'Data model' }).click()

    await expect(page.getByRole('heading', { name: 'Data model' })).toBeVisible()
    const master = page.locator('.box', { hasText: 'STUDENT MASTER' })
    await expect(master).toContainText('1,536 rows')

    // The enquiry sheet lives in the OTHER workbook, so the node is honestly
    // empty rather than drawn as if it had data.
    const enquiry = page.locator('.box', { hasText: 'ENQUIRY' }).first()
    await expect(enquiry).toHaveAttribute('data-state', /missing|partial/)

    // Nothing in either workbook carries age, area or pincode.
    const admission = page.locator('.box', { hasText: 'ADMISSION' }).last()
    await expect(admission.locator('.chip.miss')).toContainText(['age'])
  })

  test('the page is usable in dark mode and at a laptop width', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.setViewportSize({ width: 1280, height: 800 })
    await upload(page, QUESTION)

    // A transparent body would borrow the host background and read as broken.
    const bg = await page.evaluate(() =>
      getComputedStyle(document.body).backgroundColor)
    expect(bg).not.toBe('rgba(0, 0, 0, 0)')

    // Nothing may push the page sideways at this width.
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)
  })

  test('no console errors during a full run', async ({ page }) => {
    const errors = []
    page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()) })
    page.on('pageerror', (err) => errors.push(String(err)))

    await upload(page, QUESTION)
    await page.getByRole('button', { name: 'Run everything' }).click()
    await expect(page.locator('.node[data-status="done"]'))
      .toHaveCount(10, { timeout: 120_000 })

    expect(errors, errors.join('\n')).toHaveLength(0)
  })
})
