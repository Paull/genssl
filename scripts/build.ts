import { unlink } from 'node:fs/promises';

const result = await Bun.build({
  entrypoints: ['frontend/main.ts'],
  outdir: 'web',
  target: 'browser',
  minify: false,
  naming: { entry: 'app.[ext]', chunk: '[name]-[hash].[ext]', asset: '[name].[ext]' },
});
if (!result.success) {
  console.error(result.logs);
  process.exit(1);
}
const css = Bun.file('web/app.css');
if (await css.exists()) {
  await Bun.write('web/styles.css', css);
  await unlink('web/app.css').catch(() => undefined);
}
console.log('Built web/app.js and web/styles.css');
