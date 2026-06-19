const esbuild = require('esbuild');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const isDev = args.includes('--dev');
const isWatch = args.includes('--watch');

const jsOutdir = path.resolve(__dirname, '..', 'specivo', 'static', 'dist', 'js');
const cssOutdir = path.resolve(__dirname, '..', 'specivo', 'static', 'dist', 'css');

// Bundle definitions. Each entry knows its source, output dir, output filename,
// and whether it is a JS or CSS bundle. The build loop and manifest writer
// treat them uniformly.
//
// Two JS bundles, matching the historical load order: alpine-init.min.js
// registers all Alpine components/stores and must run BEFORE alpine.min.js;
// app.min.js wires the vanilla (non-Alpine) modules on DOMContentLoaded and
// runs after Alpine.
const bundles = [
  {
    type: 'js',
    entryPoints: ['js/alpine-init.js'],
    outdir: jsOutdir,
    outfile: path.join(jsOutdir, 'alpine-init.min.js'),
  },
  {
    type: 'js',
    entryPoints: ['js/app.js'],
    outdir: jsOutdir,
    outfile: path.join(jsOutdir, 'app.min.js'),
  },
  {
    type: 'css',
    entryPoints: ['css/specivo.css'],
    outdir: cssOutdir,
    outfile: path.join(cssOutdir, 'specivo.min.css'),
  },
];

const sharedOptions = {
  bundle: true,
  minify: !isDev,
  sourcemap: isDev,
  target: ['es2018'],
  // Font @font-face url()s are absolute (/static/...). Keep esbuild from trying
  // to resolve/relocate them by treating /static paths as external.
  external: ['/static/*'],
};

function contentHash(filePath) {
  const content = fs.readFileSync(filePath);
  return crypto.createHash('md5').update(content).digest('hex').slice(0, 8);
}

function cleanOldHashed(outdir, baseName) {
  const ext = path.extname(baseName);
  const stem = baseName.slice(0, -ext.length);
  const pattern = new RegExp(`^${stem}\\.[0-9a-f]{8}${ext.replace('.', '\\.')}$`);
  for (const file of fs.readdirSync(outdir)) {
    if (pattern.test(file)) {
      fs.unlinkSync(path.join(outdir, file));
    }
  }
}

function writeManifest(outdir, entries) {
  const manifest = {};
  for (const entry of entries) {
    const baseName = path.basename(entry.outfile);
    const filePath = entry.outfile;
    if (!fs.existsSync(filePath)) continue;

    const hash = contentHash(filePath);
    const ext = path.extname(baseName);
    const stem = baseName.slice(0, -ext.length);
    const hashedName = `${stem}.${hash}${ext}`;

    cleanOldHashed(outdir, baseName);
    fs.copyFileSync(filePath, path.join(outdir, hashedName));

    manifest[baseName] = hashedName;
  }

  const manifestPath = path.join(outdir, 'manifest.json');
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n');
  console.log(`Wrote ${path.relative(process.cwd(), manifestPath)}:`, manifest);
}

async function build() {
  // Ensure output dirs exist.
  for (const bundle of bundles) {
    fs.mkdirSync(bundle.outdir, { recursive: true });
  }

  for (const bundle of bundles) {
    const opts = {
      ...sharedOptions,
      entryPoints: bundle.entryPoints,
      outfile: bundle.outfile,
    };

    if (isWatch) {
      const ctx = await esbuild.context(opts);
      await ctx.watch();
      console.log(`Watching ${bundle.entryPoints[0]}...`);
    } else {
      await esbuild.build(opts);
      console.log(`Built ${path.relative(process.cwd(), bundle.outfile)}`);
    }
  }

  if (!isWatch) {
    // One manifest per output dir (so the template tag can load each
    // independently and asset types stay decoupled).
    const byOutdir = new Map();
    for (const bundle of bundles) {
      if (!byOutdir.has(bundle.outdir)) byOutdir.set(bundle.outdir, []);
      byOutdir.get(bundle.outdir).push(bundle);
    }
    for (const [outdir, entries] of byOutdir.entries()) {
      writeManifest(outdir, entries);
    }
    console.log(isDev ? 'Dev build complete.' : 'Production build complete.');
  }
}

build().catch((err) => {
  console.error(err);
  process.exit(1);
});
