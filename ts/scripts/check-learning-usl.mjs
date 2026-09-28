import { readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { Either } from 'effect'
import { decodeHub, projectHub } from '../dist/src/domain/LearningHub.js'
const [root, receiptPath] = process.argv.slice(2)
if (!root) throw new Error('Usage: node ts/scripts/check-learning-usl.mjs /path/to/built/USL [new-receipt.json]')
const adapter = resolve(root, 'dist/src/integrations/property-graph.js')
const { adaptPropertyGraph } = await import(pathToFileURL(adapter).href)
const source = JSON.parse(await readFile(new URL('../config/learning-hub.json', import.meta.url), 'utf8'))
const projected = projectHub(Either.getOrThrow(decodeHub(source)))
const input = JSON.stringify(projected.usl)
const result = adaptPropertyGraph(input, { namespace: 'metahumotonic.public' })
if (result._tag === 'Left') throw result.left
const receipt = { schema: 'metahumotonic/learning-usl-check@1', observedAt: new Date().toISOString(), status: 'PASS',
  scope: 'native property-graph adaptation; no resolver reads or authority admission',
  sourceDigest: projected.sourceDigest, adapter: result.right.source.adapter,
  adapterSha256: createHash('sha256').update(await readFile(adapter)).digest('hex'),
  nodes: Object.keys(result.right.identities.resources).length, relationships: Object.keys(result.right.identities.links).length }
if (receiptPath) await writeFile(receiptPath, JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx' })
console.log(JSON.stringify(receipt))
