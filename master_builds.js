// Every hero's 12-slot elite build, per region, exactly as the site's build
// calculator loads it.
//
//     node master_builds.js OUT.json
//
// Runs loadEliteBuild() and relatedIds() lifted verbatim from docs/index.html
// against docs/data.json and docs/calc/items.json, so the master image can
// never drift from the site: change the selection rules there and the image
// follows on the next run. Called by make_master.py; zero API calls.
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const out = process.argv[2] || path.join(ROOT, 'output', 'master_builds.json');
const html = fs.readFileSync(path.join(ROOT, 'docs', 'index.html'), 'utf8');

// Source of a top-level function, by brace matching from its declaration.
function lift(decl){
  const s = html.indexOf(decl);
  if (s < 0) throw new Error('docs/index.html no longer has `' + decl + '`');
  let depth = 0;
  for (let i = html.indexOf('{', s); i < html.length; i++){
    if (html[i] === '{') depth++;
    else if (html[i] === '}' && --depth === 0) return html.slice(s, i + 1);
  }
  throw new Error('unbalanced braces after `' + decl + '`');
}
const constant = name => {
  // matches both `const SLOTS = 12` and the `, MAX_ACTIVE = 4` that follows it
  const m = html.match(new RegExp('(?:\\bconst\\s+|,\\s*)' + name + '\\s*=\\s*(\\d+)'));
  if (!m) throw new Error('docs/index.html no longer defines ' + name);
  return Number(m[1]);
};

const DATA = JSON.parse(fs.readFileSync(path.join(ROOT, 'docs', 'data.json'), 'utf8'));
const items = JSON.parse(fs.readFileSync(path.join(ROOT, 'docs', 'calc', 'items.json'), 'utf8'));
const CALC = {items, byId: Object.fromEntries(items.map(i => [i.id, i]))};
const SLOTS = constant('SLOTS'), MAX_ACTIVE = constant('MAX_ACTIVE');
const EARLY_BUDGET = constant('EARLY_BUDGET');
let region = null, build = null, _related = null;
const R = () => DATA.regions[region];
const levelForSouls = () => 0;          // the image has no level readout

// Direct eval, so the lifted code closes over DATA, CALC, build, region and
// _related above. Strict mode gives eval its own scope for declarations, hence
// the explicit return.
/* eslint-disable no-eval */
const {loadEliteBuild} = eval('(() => {' + lift('function relatedIds(){') + '\n' +
                              lift('function loadEliteBuild(){') +
                              '\nreturn {relatedIds, loadEliteBuild};})()');

const result = {generated_at: DATA.generated_at, regions: {}};
let filled = 0, total = 0;
for (const rg of Object.keys(DATA.regions)){
  region = rg;
  result.regions[rg] = {};
  for (const slug of Object.keys(DATA.heroes)){
    build = {slug, slots: [], fx: new Set()};
    loadEliteBuild();
    result.regions[rg][slug] = build.slots.map(id => {
      total++;
      if (id == null) return null;
      filled++;
      const it = CALC.byId[id];
      return {id, name: it.name, cat: it.cat, tier: it.tier, icon: it.icon[0]};
    });
  }
}
fs.mkdirSync(path.dirname(out), {recursive: true});
fs.writeFileSync(out, JSON.stringify(result));
console.error('  [master] %d heroes x %d regions, %d of %d slots filled',
              Object.keys(DATA.heroes).length, Object.keys(DATA.regions).length,
              filled, total);
