#!/usr/bin/env node
import esbuild from 'esbuild';
import { chmodSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(__dirname, '..');
const distDir = resolve(rootDir, 'dist');
const outFile = resolve(distDir, 'index.js');

mkdirSync(distDir, { recursive: true });

const start = performance.now();

try {
  await esbuild.build({
    entryPoints: [resolve(rootDir, 'src/entry.tsx')],
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node22',
    outfile: outFile,
    banner: {
      js: '#!/usr/bin/env node\nimport { createRequire } from "node:module";\nconst require = createRequire(import.meta.url);',
    },
    packages: 'external',
    sourcemap: true,
  });

  chmodSync(outFile, 0o755);
  const elapsed = (performance.now() - start).toFixed(1);
  console.log(`✓ Compiled ui-tui to dist/index.js in ${elapsed}ms`);
} catch (err) {
  console.error('✗ Build failed:', err);
  process.exit(1);
}
