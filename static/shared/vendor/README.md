# Vendored libraries

Copied as ES modules from the npm tarballs, so the pages and module workers can import them
with no build step (module workers can't use import maps). Each folder keeps its licence.

| Folder | Package | Version | Licence | File copied |
|--------|---------|---------|---------|-------------|
| `pbf/` | [pbf](https://github.com/mapbox/pbf) | 5.1.2 | BSD-3-Clause | `index.js` |
| `vector-tile/` | [@mapbox/vector-tile](https://github.com/mapbox/vector-tile-js) | 3.0.0 | BSD-3-Clause | `index.js` |
| `point-geometry/` | [@mapbox/point-geometry](https://github.com/mapbox/point-geometry) | 1.1.0 | ISC | `index.js` |
| `earcut/` | [earcut](https://github.com/mapbox/earcut) | 3.2.4 | ISC | `src/earcut.js` |

Changes from the published files:

- `vector-tile/index.js`: `import Point from '@mapbox/point-geometry'` became
  `import Point from '../point-geometry/index.js'`. Nothing else changed.
- `pbf/`: only `index.js` (the reader and writer). Its `compile.js`, which needs
  `resolve-protobuf-schema`, is not copied.

To update one: `npm pack <package>@<version>`, copy the same file, redo the import rewrite above,
and update this table.
