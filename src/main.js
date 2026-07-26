/* InNetwork app — the page's interactive layer (state, network, map, search, UI).
 *
 * Bundled by build.mjs (esbuild) into innetwork.bundle.js and loaded by innetwork.html
 * as a single same-origin <script src>, so the page carries no inline business logic
 * (which lets the CSP drop 'unsafe-inline' for scripts — see D3). Deploy config comes
 * from ./config.js (injected window.INNETWORK_CONFIG); pure transforms come from the
 * shared, unit-tested ../innetwork.logic.js.
 */
import {
  API_BASE,
  HAS_BACKEND,
  ALLOW_PUBLIC_PROXIES,
  CLAIM_EMAIL,
  CLAIM_ENABLED,
  NPI_API,
  NOMINATIM,
  SERVED,
  SAME_ORIGIN,
} from './config.js';
import logic from '../innetwork.logic.js';

const {
  TAXONOMY_MAP,
  haversine,
  cssEsc,
  esc,
  buildNpiParams,
  buildProviders,
  adaptBackendProvider,
  coverageStatus,
  fmtDate,
  csvCell,
} = logic;

const LS_KEY = 'innetwork_saved_v4';
const GEO_KEY = 'innetwork_geocache_v1';

// Loosely-typed element accessor: the app knows which concrete element each id is, so
// this returns `any` to let `tsc --checkJs` allow .value/.dataset/etc. at DOM boundaries
// without a cast per site. checkJs still verifies our own logic, flow, and call signatures.
/** @param {string} id @returns {any} */
const byId = (id) => document.getElementById(id);

const CHECK_SVG =
  '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
const US_STATES = [
  'AL',
  'AK',
  'AZ',
  'AR',
  'CA',
  'CO',
  'CT',
  'DE',
  'FL',
  'GA',
  'HI',
  'ID',
  'IL',
  'IN',
  'IA',
  'KS',
  'KY',
  'LA',
  'ME',
  'MD',
  'MA',
  'MI',
  'MN',
  'MS',
  'MO',
  'MT',
  'NE',
  'NV',
  'NH',
  'NJ',
  'NM',
  'NY',
  'NC',
  'ND',
  'OH',
  'OK',
  'OR',
  'PA',
  'RI',
  'SC',
  'SD',
  'TN',
  'TX',
  'UT',
  'VT',
  'VA',
  'WA',
  'WV',
  'WI',
  'WY',
  'DC',
  'PR',
];

/* ════════════════════════════════════════════
   LEAFLET LOADER (CDN fallbacks, map is optional)
   ════════════════════════════════════════════ */
const LEAFLET_CSS = [
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css',
];
const LEAFLET_JS = [
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js',
];
// Optional clustering so a dense result set doesn't overlap into an unreadable mass.
const CLUSTER_CSS = [
  'https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css',
];
const CLUSTER_CSS2 = [
  'https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.min.css',
];
const CLUSTER_JS = [
  'https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js',
];
let leafletOk = false;
function loadStylesheet(urls) {
  return new Promise((res) => {
    let i = 0;
    (function n() {
      if (i >= urls.length) return res(false);
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = urls[i++];
      l.onload = () => res(true);
      l.onerror = () => {
        l.remove();
        n();
      };
      document.head.appendChild(l);
    })();
  });
}
function loadScriptChain(urls) {
  return new Promise((res) => {
    let i = 0;
    (function n() {
      if (i >= urls.length) return res(false);
      const s = document.createElement('script');
      s.src = urls[i++];
      s.async = false;
      s.onload = () => res(true);
      s.onerror = () => {
        s.remove();
        n();
      };
      document.head.appendChild(s);
    })();
  });
}
const leafletReady = (async () => {
  if (!window.L) {
    loadStylesheet(LEAFLET_CSS);
    const ok = await loadScriptChain(LEAFLET_JS);
    leafletOk = ok && !!window.L;
  } else leafletOk = true;
  // Clustering is best-effort: if it fails to load, markers just plot individually.
  if (leafletOk && !window.L.markerClusterGroup) {
    loadStylesheet(CLUSTER_CSS);
    loadStylesheet(CLUSTER_CSS2);
    await loadScriptChain(CLUSTER_JS);
  }
  return leafletOk;
})();

/* ════════════════════════════════════════════
   STATE
   ════════════════════════════════════════════ */
const state = {
  providers: [],
  favorites: {},
  activeNpi: null,
  activeTab: 'results',
  center: null,
  centerLabel: '',
  searching: false,
  token: 0,
  sort: 'relevance',
  geocache: {},
  plans: [],
  categories: [],
  selectedPlans: [],
  // Plan-picker state. `planState` is the 2-letter code the plan list is currently scoped
  // to (''=national list); `planQuery` is the picker's type-to-filter text. Both exist
  // because plan-level coverage runs to thousands of entries — see renderInsuranceFilter.
  planState: '',
  planQuery: '',
  radius: 0,
  backendReachable: null,
};
let mapInstance = null,
  mapMarkers = {},
  centerMarker = null,
  markerClusterLayer = null,
  tileLayer = null;

/* ════════════════════════════════════════════
   THEME (light / dark) — persisted, system-aware
   ════════════════════════════════════════════ */
const THEME_KEY = 'innetwork_theme';
// Brand-matched tiles per theme: CARTO Voyager (light) / Dark Matter (dark).
const TILES = {
  light: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
  dark: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
};
const THEME_COLOR = { light: '#0d241d', dark: '#060f0b' };
function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}
function reducedMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}
// Run a DOM update inside a View Transition for a smooth crossfade, when supported and
// motion is allowed; otherwise update immediately. Used for tab + list/map swaps.
function withVT(fn) {
  if (document.startViewTransition && !reducedMotion()) document.startViewTransition(fn);
  else fn();
}
// Set the header results count with a quick "pop" so a new total feels alive.
function setResultsCount(text) {
  const el = byId('results-count-header');
  if (!el) return;
  el.textContent = text;
  if (text && !reducedMotion()) {
    el.classList.remove('count-pop');
    void el.offsetWidth; // restart the animation
    el.classList.add('count-pop');
  }
}
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLOR[theme]);
  // Re-skin the map tiles to match without rebuilding the map.
  if (mapInstance && tileLayer) tileLayer.setUrl(TILES[theme]);
}
function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (_) {}
  applyTheme(next);
}

/* ════════════════════════════════════════════
   NETWORK — same-origin when served, resilient fallback for file://
   ════════════════════════════════════════════ */
function npiCandidates(params) {
  const qs = params.toString();
  const full = NPI_API + '?' + qs;
  const list = [];
  if (HAS_BACKEND) list.push(API_BASE + '/api/npi?' + qs); // your backend: cached, private
  if (SERVED) list.push(SAME_ORIGIN + '/api/npi?' + qs); // hosted: same origin, no CORS, cached
  list.push('http://localhost:8787/api/npi?' + qs); // local helper if running
  list.push(NPI_API + '?' + qs); // NPPES directly — note: it does NOT send CORS headers, so this only succeeds server-side, not from a browser
  if (ALLOW_PUBLIC_PROXIES) {
    // opt-in only — the real standalone path; see privacy note above
    list.push('https://api.codetabs.com/v1/proxy/?quest=' + encodeURIComponent(full));
    list.push('https://api.allorigins.win/raw?url=' + encodeURIComponent(full));
    list.push('https://corsproxy.io/?url=' + encodeURIComponent(full));
  }
  return [...new Set(list)];
}

async function fetchNpi(params) {
  let _lastErr = null;
  for (const url of npiCandidates(params)) {
    const local = url.includes('localhost') || url.startsWith(SAME_ORIGIN + '/api');
    try {
      const res = await fetch(url, {
        headers: { Accept: 'application/json' },
        signal: AbortSignal.timeout(local ? 5000 : 18000),
      });
      if (!res.ok) {
        _lastErr = new Error('Gateway returned ' + res.status + '.');
        continue;
      }
      const text = await res.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        _lastErr = new Error('Gateway returned an unexpected response.');
        continue;
      }
      if (data.Errors && data.Errors.length)
        throw new Error(data.Errors[0].description || 'The registry rejected the query.');
      if (!('results' in data) && !('result_count' in data)) {
        _lastErr = new Error('Unexpected registry response.');
        continue;
      }
      return data.results || [];
    } catch (e) {
      if (e && e.message && /rejected the query/.test(e.message)) throw e;
      _lastErr = e;
    }
  }
  throw new Error(
    'Could not reach the registry directly from your browser (the CMS registry does not allow direct browser calls). Start the InNetwork backend — "uvicorn app.main:app --port 8000" — and open http://localhost:8000, which queries the registry server-side.',
  );
}

async function geocode(query) {
  // query: {postalcode,...} or {q,...}
  const params = new URLSearchParams({ ...query, format: 'json', limit: '1', countrycodes: 'us' });
  // Try the backend proxy first (cached, polite); fall through to Nominatim
  // directly from the browser if the proxy can't resolve it. Each candidate is
  // tried in turn — a null/blocked answer from one moves on to the next.
  const tries = [];
  if (HAS_BACKEND) tries.push(API_BASE + '/api/geocode?' + params.toString());
  if (SERVED && SAME_ORIGIN && SAME_ORIGIN !== API_BASE) tries.push(SAME_ORIGIN + '/api/geocode?' + params.toString());
  tries.push('http://localhost:8787/api/geocode?' + params.toString());
  tries.push(NOMINATIM + '/search?' + params.toString()); // Nominatim sends CORS headers
  for (const url of [...new Set(tries)]) {
    try {
      const res = await fetch(url, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(9000) });
      if (!res.ok) continue;
      const data = await res.json();
      // Backend proxy shape: {coords:[lat,lon]} or {coords:null}
      if (data && typeof data === 'object' && !Array.isArray(data) && 'coords' in data) {
        if (Array.isArray(data.coords) && data.coords.length >= 2)
          return [parseFloat(data.coords[0]), parseFloat(data.coords[1])];
        continue; // null coords here — try the next source
      }
      // Raw Nominatim shape: [{lat, lon}, ...]
      if (Array.isArray(data) && data.length) return [parseFloat(data[0].lat), parseFloat(data[0].lon)];
    } catch (_) {
      /* next */
    }
  }
  return null;
}

async function reverseGeocode(lat, lon) {
  const params = new URLSearchParams({ lat: String(lat), lon: String(lon), format: 'json' });
  const tries = [];
  if (SERVED) tries.push(SAME_ORIGIN + '/api/reverse?' + params.toString());
  tries.push('http://localhost:8787/api/reverse?' + params.toString());
  tries.push(NOMINATIM + '/reverse?' + params.toString());
  for (const url of tries) {
    try {
      const res = await fetch(url, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(9000) });
      const data = await res.json();
      if (data && data.address) return data.address.postcode || '';
    } catch (_) {}
  }
  return '';
}

/* ════════════════════════════════════════════
   MAP
   ════════════════════════════════════════════ */
function initMap() {
  const el = byId('map');
  if (!el) return;
  if (!el.style.height || el.offsetHeight === 0) {
    el.style.height = '100%';
    el.style.minHeight = '360px';
  }
  try {
    mapInstance = L.map('map', { center: [39.5, -98.35], zoom: 4, zoomControl: true, attributionControl: true });
    tileLayer = L.tileLayer(TILES[currentTheme()], {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    }).addTo(mapInstance);
    // Cluster dense pins when the plugin is available; otherwise markers go on the map directly.
    markerClusterLayer = L.markerClusterGroup
      ? L.markerClusterGroup({
          maxClusterRadius: 50,
          showCoverageOnHover: false,
          spiderfyOnMaxZoom: true,
          chunkedLoading: true,
        }).addTo(mapInstance)
      : null;
    setTimeout(() => {
      if (mapInstance) mapInstance.invalidateSize();
    }, 280);
  } catch (e) {
    console.error('Leaflet init failed:', e);
    mapInstance = null;
  }
}
// Add a marker to the cluster layer when present, else straight onto the map.
function _addMarker(m) {
  if (markerClusterLayer) markerClusterLayer.addLayer(m);
  else m.addTo(mapInstance);
}
function createMarkerIcon(color, active) {
  return L.divIcon({
    className: '',
    html: `<div class="marker-pin ${active ? 'active' : ''}" style="background:${color};"></div>`,
    iconSize: active ? [34, 34] : [26, 26],
    iconAnchor: active ? [17, 34] : [13, 26],
    popupAnchor: [0, active ? -32 : -24],
  });
}
function plotMarkers() {
  if (!mapInstance) return;
  clearMarkers();
  if (state.center) {
    centerMarker = L.marker(state.center, {
      icon: L.divIcon({
        className: '',
        html: '<div class="center-pin"></div>',
        iconSize: [18, 18],
        iconAnchor: [9, 9],
      }),
      interactive: false,
      keyboard: false,
      zIndexOffset: -100,
    }).addTo(mapInstance);
  }
  const docs = state.providers.filter((d) => d.lat && d.lng);
  const bounds = L.latLngBounds(state.center ? [state.center] : []);
  docs.forEach((doc) => {
    // title/alt give the keyboard-focusable marker an accessible name (WCAG aria-command-name, D2).
    const m = L.marker([doc.lat, doc.lng], {
      icon: createMarkerIcon(doc.color, false),
      title: doc.name,
      alt: `${doc.name} — ${doc.specialty}`,
    });
    m.bindPopup(buildPopup(doc), { maxWidth: 280, className: 'innetwork-popup' });
    m.on('click', () => selectDoctor(doc.npi, false));
    m.on('popupopen', () => {
      updateMarkerStyles(doc.npi);
      highlightCard(doc.npi);
    });
    _addMarker(m);
    mapMarkers[doc.npi] = m;
    bounds.extend([doc.lat, doc.lng]);
  });
  if (bounds.isValid()) mapInstance.fitBounds(bounds, { padding: [50, 50], maxZoom: 14 });
  else if (state.center) mapInstance.setView(state.center, 11);
  if (docs.length) showPill(`${docs.length} of ${state.providers.length} mapped`);
}
function addOrMoveMarker(doc) {
  if (!mapInstance || !doc.lat || !doc.lng) return;
  if (mapMarkers[doc.npi]) {
    mapMarkers[doc.npi].setLatLng([doc.lat, doc.lng]).setPopupContent(buildPopup(doc));
    return;
  }
  const m = L.marker([doc.lat, doc.lng], {
    icon: createMarkerIcon(doc.color, false),
    title: doc.name,
    alt: `${doc.name} — ${doc.specialty}`,
  });
  m.bindPopup(buildPopup(doc), { maxWidth: 280, className: 'innetwork-popup' });
  m.on('click', () => selectDoctor(doc.npi, false));
  m.on('popupopen', () => {
    updateMarkerStyles(doc.npi);
    highlightCard(doc.npi);
  });
  _addMarker(m);
  mapMarkers[doc.npi] = m;
}
function clearMarkers() {
  if (markerClusterLayer) markerClusterLayer.clearLayers();
  Object.values(mapMarkers).forEach((m) => m.remove());
  mapMarkers = {};
  if (centerMarker) {
    centerMarker.remove();
    centerMarker = null;
  }
}
function updateMarkerStyles(activeNpi) {
  state.activeNpi = activeNpi;
  state.providers.forEach((doc) => {
    const m = mapMarkers[doc.npi];
    if (!m) return;
    const a = doc.npi === activeNpi;
    m.setIcon(createMarkerIcon(doc.color, a));
    m.setZIndexOffset(a ? 1000 : 0);
  });
}
function buildPopup(doc) {
  const dir = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(doc.fullAddress || doc.name)}`;
  return `<div>
    <div class="popup-name">${esc(doc.name)}</div>
    <div class="popup-specialty">${esc(doc.specialty)}</div>
    <div class="popup-row"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>${esc(doc.fullAddress || 'Address unavailable')}</div>
    ${doc.phone ? `<div class="popup-row"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3-8.6A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.7a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.4-1.1a2 2 0 0 1 2.1-.5c.9.3 1.8.5 2.7.6a2 2 0 0 1 1.7 2Z"/></svg>${esc(doc.phone)}</div>` : ''}
    <div class="popup-actions">${doc.phone ? `<a href="tel:${esc(doc.phoneRaw)}">Call</a>` : ''}<a href="${dir}" target="_blank" rel="noopener">Directions</a></div>
  </div>`;
}
function showMapUnavailable() {
  const c = byId('map-container');
  if (!c) return;
  let el = byId('map-unavailable');
  if (!el) {
    el = document.createElement('div');
    el.id = 'map-unavailable';
    c.appendChild(el);
  }
  el.style.cssText =
    'position:absolute;inset:0;z-index:600;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:11px;padding:2rem;background:var(--paper-2);color:var(--muted);';
  el.innerHTML = `<svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="#8a958f" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/><path d="M2 2l20 20"/></svg>
    <p style="font-family:'Fraunces',serif;font-size:1.05rem;font-weight:600;color:var(--ink);margin:0;">Map couldn't load</p>
    <p style="font-size:.83rem;line-height:1.55;margin:0;max-width:36ch;">Your results still appear in the list. The map needs the Leaflet library from a CDN, which your network may be blocking.</p>
    <button class="state-retry" data-action="retry-map">Retry map</button>`;
}
function hideMapUnavailable() {
  const el = byId('map-unavailable');
  if (el) el.remove();
}
async function retryMap() {
  hideMapUnavailable();
  if (!window.L) leafletOk = (await loadScriptChain(LEAFLET_JS)) && !!window.L;
  else leafletOk = true;
  if (leafletOk) {
    initMap();
    if (mapInstance) {
      if (state.center) mapInstance.setView(state.center, 11);
      if (state.providers.length) plotMarkers();
      return;
    }
  }
  showMapUnavailable();
}

/* ════════════════════════════════════════════
   SEARCH
   ════════════════════════════════════════════ */
function readForm() {
  return {
    zip: byId('zip-input').value.trim(),
    radius: byId('radius-select').value,
    specialty: byId('specialty-select').value,
    name: byId('name-input').value.trim(),
    city: byId('city-input').value.trim(),
    st: byId('state-select').value,
    npi: byId('npi-input').value.trim(),
    type: byId('type-select').value,
    limit: byId('limit-select').value,
  };
}

async function handleSearch() {
  if (!mapInstance && leafletOk) initMap();
  const f = readForm();

  if (f.npi && !/^\d{10}$/.test(f.npi)) {
    shake(byId('npi-input'));
    showToast('An NPI is exactly 10 digits.');
    return;
  }
  if (!f.npi && !f.zip && !(f.city && f.st)) {
    shake(byId('zip-input'));
    showToast('Enter a ZIP code, or a city and state, or an NPI.');
    return;
  }
  if (f.zip && !/^\d{5}$/.test(f.zip)) {
    shake(byId('zip-input'));
    showToast('Enter a valid 5-digit ZIP code.');
    return;
  }
  if (state.searching) return;

  const token = ++state.token;
  state.radius = !f.npi && f.radius ? parseInt(f.radius, 10) || 0 : 0;
  state.searching = true;
  setSearchLoading(true);
  clearMarkers();
  state.providers = [];
  state.activeNpi = null;
  state.searchMeta = null;
  switchTabSilent('results');
  writeUrl(f);

  try {
    // Center for the map + distances
    state.center = null;
    state.centerLabel = '';
    if (f.zip) {
      state.center = await geocode({ postalcode: f.zip });
      state.centerLabel = f.zip;
    } else if (f.city && f.st) {
      state.center = await geocode({ city: f.city, state: f.st });
      state.centerLabel = `${f.city}, ${f.st}`;
    }
    if (state.center && mapInstance) mapInstance.setView(state.center, 11, { animate: true });

    if (HAS_BACKEND) {
      // One call: NPPES + real insurance flags + server-side batched geocoding.
      let result = null,
        backendUnreachable = false;
      try {
        result = await backendSearch(f);
      } catch (err) {
        if (!(err && err.unreachable)) throw err;
        backendUnreachable = true;
      }
      if (result !== null) {
        if (token !== state.token) return;
        const providers = result.providers;
        if (!providers.length) {
          showEmptyState(describeNoResults(f));
          finishSearch();
          return;
        }
        state.providers = providers;
        state.searchMeta = result.meta;
        applySort();
        renderCards();
        if (mapInstance) plotMarkers(); // coords are already attached — pins now, no wait
        setResultsCount(`${providers.length} provider${providers.length !== 1 ? 's' : ''} found`);
        finishSearch();
        // Narrow the plan picker to where the user is actually searching. Deliberately
        // after finishSearch(): results must never wait on a plan-list refresh.
        rescopePlans(inferSearchState(f, providers));
        return;
      }
      // Backend unreachable. The CMS registry does NOT allow direct browser calls
      // (CORS-blocked), so the only standalone path that actually works is the
      // opt-in public-proxy route. Without it, don't pretend to search — tell the
      // user plainly to start the backend rather than failing cryptically.
      if (backendUnreachable && !ALLOW_PUBLIC_PROXIES) {
        if (token !== state.token) return;
        showBackendRequired();
        finishSearch();
        return;
      }
      if (backendUnreachable) noteBackendFallback();
    }

    const results = await fetchNpi(buildNpiParams(f));
    if (token !== state.token) return;

    if (!results.length) {
      showEmptyState(describeNoResults(f));
      finishSearch();
      return;
    }
    state.providers = buildProviders(results);
    applySort();
    renderCards();
    if (mapInstance) plotMarkers();
    setResultsCount(`${state.providers.length} provider${state.providers.length !== 1 ? 's' : ''} found`);
    geocodeProviders(token);
  } catch (err) {
    if (token !== state.token) return;
    console.error('Search error:', err);
    showErrorState(err.message || 'Network error. Check your connection and try again.');
  }
  finishSearch();
}

/* Backend-powered search: returns adapted provider objects with real
   insurance flags and coordinates already attached (geocoded server-side). */
async function backendSearch(f) {
  // geocode=true: the server geocodes the candidate pool, distance-filters to the
  // radius, sorts by distance, and returns ranked providers with coordinates already
  // attached — so pins are placed on this first response (no client-side re-geocode).
  const p = new URLSearchParams({ limit: f.limit, geocode: 'true' });
  if (f.radius && !f.npi) p.set('radius', f.radius);
  if (f.npi) p.set('npi', f.npi);
  if (f.zip) p.set('zip', f.zip);
  if (f.city) p.set('city', f.city);
  if (f.st) p.set('state', f.st);
  if (f.specialty && TAXONOMY_MAP[f.specialty]) p.set('taxonomy', TAXONOMY_MAP[f.specialty]);
  if (f.type) p.set('type', f.type);
  if (f.name) p.set('name', f.name);
  if (state.selectedPlans.length) {
    // Verified-only filtering — every surfaced plan is a verified source; the estimated
    // ('any') match mode was removed with the estimated tier (backend defaults to verified).
    p.set('accepts', state.selectedPlans.join(','));
  }
  let res;
  try {
    res = await fetch(`${API_BASE}/api/providers/search?${p.toString()}`, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(30000),
    });
  } catch (_) {
    // Connection refused / DNS / timeout — backend isn't reachable.
    const e = /** @type {Error & { unreachable?: boolean }} */ (new Error('The InNetwork API is unreachable.'));
    e.unreachable = true;
    throw e;
  }
  if (!res.ok) {
    if (res.status === 400) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || 'The registry rejected the query.');
    }
    const e = /** @type {Error & { unreachable?: boolean }} */ (new Error('The InNetwork API is unreachable.'));
    e.unreachable = true;
    throw e;
  }
  const data = await res.json();
  return {
    providers: (data.providers || []).map((p) => adaptBackendProvider(p, state.center)),
    // Truncation metadata so the results bar can honestly say "showing N of M"
    // instead of silently dropping the rest of the pool (backend T1.4).
    meta: { total: data.total, truncated: !!data.truncated, poolCapped: !!data.pool_capped },
  };
}

// Tell the user (once per session) we're querying the official registry directly
// because the optional InNetwork backend isn't running.
let _backendFallbackNoted = false;
function noteBackendFallback() {
  // Only reached when ALLOW_PUBLIC_PROXIES is on — the one standalone path that
  // actually reaches the registry from a browser. (Without it, the search shows the
  // honest "start the backend" state instead of this.)
  if (_backendFallbackNoted) return;
  _backendFallbackNoted = true;
  console.info('InNetwork backend not reachable — routing through the opt-in public CORS proxy.');
  showToast('Backend offline — using the public CORS proxy');
}

// Probe the backend once on load so we can tell the user upfront (not just on search)
// when provider search won't work because the backend isn't running.
async function probeBackend() {
  if (!HAS_BACKEND) {
    state.backendReachable = false;
  } else {
    try {
      state.backendReachable = (await fetch(`${API_BASE}/healthz`, { signal: AbortSignal.timeout(4000) })).ok;
    } catch (_) {
      state.backendReachable = false;
    }
  }
  if (
    state.backendReachable === false &&
    !ALLOW_PUBLIC_PROXIES &&
    state.activeTab === 'results' &&
    !state.providers.length &&
    !state.searching
  ) {
    showBackendRequired();
  }
}

function finishSearch() {
  setSearchLoading(false);
  state.searching = false;
}

/* ── Insurance plans (real, from the backend) ── */
async function loadPlans(st) {
  if (!HAS_BACKEND) return;
  const scope = (st || '').trim().toUpperCase();
  try {
    const qs = scope ? `?state=${encodeURIComponent(scope)}` : '';
    const res = await fetch(`${API_BASE}/api/insurance/plans${qs}`, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(8000),
    });
    const data = await res.json();
    state.plans = data.plans || [];
    state.categories = data.categories || [];
    state.planState = scope;
  } catch (_) {
    // A failed fetch is not evidence that there are no plans, so it must never CLEAR the
    // ones already loaded. This matters because of re-scoping: the list is refetched after
    // every search, and clobbering it on a transient failure would make the whole insurance
    // filter — including the user's active selections — vanish mid-session. On the very
    // first load there is nothing to keep, so this still degrades to an empty filter.
    state.plans = state.plans || [];
    state.categories = state.categories || [];
  }
  renderInsuranceFilter();
}

/* Re-scope the plan list to the state the user is actually searching. Plan-level coverage
 * is mostly REGIONAL — a Marketplace plan sold only in Alaska is noise for a Texan, and
 * offering it invites a filter that can only ever return nothing. Scoping happens on the
 * server (?state=), so the browser never receives thousands of irrelevant plans. Any plan
 * the user already selected is kept, even if it falls outside the new scope, so re-scoping
 * can never silently drop an active filter. */
async function rescopePlans(st) {
  const scope = (st || '').trim().toUpperCase();
  if (!HAS_BACKEND || !scope || scope === state.planState) return;
  const keep = state.selectedPlans.slice();
  await loadPlans(scope);
  state.selectedPlans = keep;
  renderInsuranceFilter();
}

/* The state to scope plans to: what the user typed, else the state most of the results are
 * in (a ZIP search never names a state, but its results do). */
function inferSearchState(f, providers) {
  if (f && f.st) return f.st;
  const counts = {};
  for (const p of providers || []) {
    const s = (p.stateAb || '').toUpperCase();
    if (s) counts[s] = (counts[s] || 0) + 1;
  }
  let best = '',
    n = 0;
  for (const s in counts)
    if (counts[s] > n) {
      best = s;
      n = counts[s];
    }
  return best;
}
function renderInsuranceFilter() {
  const field = byId('insurance-field');
  const wrap = byId('insurance-filter');
  if (!state.plans.length) {
    field.style.display = 'none';
    return;
  }
  field.style.display = 'block';
  const cats =
    state.categories && state.categories.length
      ? state.categories
      : [{ id: 'all', label: 'Plans', plans: state.plans }];
  // Verified tier only. A payer is offered as a filter solely when InNetwork can confirm
  // acceptance from a real source for each provider — a harvested membership set, a
  // live-validated FHIR directory, or the CMS Medicare file. Unverifiable "estimated"
  // catalog payers are deliberately not surfaced: the product shows what it can prove.
  // Type-to-filter, applied to the label. With plan-level coverage a category can hold
  // hundreds of plans ("Blue Cross Premier PPO Gold", "…Silver", "…Bronze Saver HSA"), and
  // scanning a scroll box for yours is hopeless — searching for it is not. A selected plan
  // always renders, whatever the query, so a filter you turned on can never hide itself.
  const q = (state.planQuery || '').trim().toLowerCase();
  const matches = (pl) => state.selectedPlans.includes(pl.id) || !q || pl.label.toLowerCase().includes(q);

  const chipFor = (pl) => {
    const on = state.selectedPlans.includes(pl.id);
    const title =
      pl.level === 'plan' ? 'Verified from a real source' : 'Verified network directory — confirm your specific plan';
    return `<button type="button" role="checkbox" aria-checked="${on ? 'true' : 'false'}" class="ins-chip ${on ? 'checked' : ''}" data-action="toggle-plan" data-plan="${esc(pl.id)}" title="${esc(title)}">
        <span class="tick">${on ? CHECK_SVG : ''}</span>${esc(pl.label)}<span class="conf-dot verified" aria-hidden="true"></span></button>`;
  };

  let shown = 0;
  const verifiedTotal = cats.reduce((n, c) => n + c.plans.filter((pl) => pl.confidence === 'verified').length, 0);
  const groups = cats
    .map((c) => {
      const plans = c.plans.filter((pl) => pl.confidence === 'verified' && matches(pl));
      if (!plans.length) return '';
      shown += plans.length;
      const gid = `ins-group-${esc(c.id)}`;
      return `<div class="ins-group" role="group" aria-labelledby="${gid}"><p class="ins-group-title" id="${gid}">${esc(c.label)} <span class="ins-group-n">${plans.length}</span></p><div class="ins-chips">${plans.map(chipFor).join('')}</div></div>`;
    })
    .filter(Boolean)
    .join('');

  // The search box only appears once the list is big enough to need it — a handful of
  // national payers stays the plain chip list it has always been.
  const NEEDS_SEARCH = 12;
  const scopeNote = state.planState ? ` in <b>${esc(state.planState)}</b>` : '';
  const search =
    verifiedTotal > NEEDS_SEARCH
      ? `<div class="ins-search"><input type="search" id="plan-search" class="ins-search-input" placeholder="Search your plan…" aria-label="Search insurance plans" value="${esc(state.planQuery || '')}" autocomplete="off" />
         <span class="ins-search-n">${shown} of ${verifiedTotal} plans${scopeNote}</span></div>`
      : '';
  const empty =
    verifiedTotal && !shown
      ? `<p class="ins-empty">No plan matches “${esc(state.planQuery)}”. <button type="button" class="linklike" data-action="clear-plan-query">Clear</button></p>`
      : '';
  // Read focus/caret from the live DOM before it is replaced below.
  const prev = byId('plan-search');
  const hadFocus = !!prev && document.activeElement === prev;
  const caret = prev && prev.selectionStart != null ? prev.selectionStart : 0;
  const legend = `<p class="ins-legend"><span class="conf-dot verified" aria-hidden="true"></span> Confirmed from a real source for each provider — never a guess.</p>`;
  wrap.innerHTML = `${legend}${search}<div class="ins-groups">${groups}</div>${empty}`;

  const box = byId('plan-search');
  if (box) {
    box.addEventListener('input', () => {
      state.planQuery = box.value;
      renderInsuranceFilter();
    });
    // Re-rendering replaces the input element, so focus and caret must be carried over or
    // typing dies after one character. The "was it focused" flag is read from the DOM
    // BEFORE the innerHTML swap (see `hadFocus` above), not tracked with a blur listener:
    // tearing down a focused node fires blur, which would clear the flag before the new
    // input could read it.
    if (hadFocus) {
      box.focus();
      const at = Math.min(caret, box.value.length);
      box.setSelectionRange(at, at);
    }
  }
}
function togglePlan(id) {
  const i = state.selectedPlans.indexOf(id);
  if (i >= 0) state.selectedPlans.splice(i, 1);
  else state.selectedPlans.push(id);
  renderInsuranceFilter();
}
function clearPlans() {
  state.selectedPlans = [];
  renderInsuranceFilter();
  handleSearch();
}
function planMeta(id) {
  return state.plans.find((x) => x.id === id) || null;
}
function planLabel(id) {
  const p = planMeta(id);
  return p ? p.label : id;
}

function describeNoResults(f) {
  if (f.npi) return `No provider is registered under NPI ${f.npi}.`;
  const where = f.zip ? `near ${f.zip}` : f.city ? `in ${f.city}${f.st ? ', ' + f.st : ''}` : '';
  const what = f.specialty ? `${f.specialty.toLowerCase()} providers` : 'providers';
  return `No ${what} are listed ${where}.`;
}

/* ════════════════════════════════════════════
   BUILD PROVIDERS — REAL FIELDS ONLY
   Standalone (no-backend) path only. It mirrors the backend's normalize()
   (app/main.py) field-for-field; when HAS_BACKEND is set, adaptBackendProvider()
   consumes the server's normalized shape instead and this is unused. Keep the two
   in sync if you add a field. buildProviders() itself lives in innetwork.logic.js
   (the normalize() mirror, shared with the unit tests).
   ════════════════════════════════════════════ */

/* ════════════════════════════════════════════
   GEOCODING — real addresses only, cached
   ════════════════════════════════════════════ */
function loadGeocache() {
  try {
    state.geocache = JSON.parse(localStorage.getItem(GEO_KEY)) || {};
  } catch {
    state.geocache = {};
  }
}
function saveGeocache() {
  try {
    localStorage.setItem(GEO_KEY, JSON.stringify(state.geocache));
  } catch (_) {}
}
function addrKey(d) {
  return `${d.address1}|${d.city}|${d.stateAb}|${d.postalCode}`.toLowerCase();
}

async function geocodeProviders(token) {
  // unique practice addresses, in result order. Skip anything already located
  // (e.g. a backend that returned coordinates) so we never re-geocode needlessly.
  const seen = new Set(),
    queue = [];
  for (const d of state.providers) {
    if (!d.address1 || !d.city || !d.stateAb) continue;
    const k = addrKey(d);
    if (d.lat != null && d.lng != null) {
      seen.add(k);
      continue;
    }
    if (seen.has(k)) continue;
    seen.add(k);
    queue.push({ key: k, sample: d });
  }
  for (const { key, sample } of queue) {
    if (token !== state.token) return;
    let coords = state.geocache[key];
    if (!coords) {
      coords = await geocode({ q: `${sample.address1}, ${sample.city}, ${sample.stateAb} ${sample.postalCode}` });
      if (coords) {
        state.geocache[key] = coords;
        saveGeocache();
      }
      await sleep(1100); // polite pacing — the free geocoder allows ~1 request/second
    }
    if (token !== state.token) return;
    if (coords) {
      state.providers.forEach((p) => {
        if (addrKey(p) === key) {
          p.lat = coords[0];
          p.lng = coords[1];
          p.geocoded = true;
          p.distance = state.center ? haversine(state.center, coords) : null;
          addOrMoveMarker(p);
          refreshCardGeo(p);
        }
      });
    }
  }
  if (token === state.token) {
    // True radius: drop providers we located outside the chosen distance. Those
    // still being located (no coords yet) are kept — we can't claim they're out.
    if (state.radius && state.center) {
      const before = state.providers.length;
      state.providers = state.providers.filter((p) => p.distance == null || p.distance <= state.radius);
      if (state.providers.length !== before) {
        clearMarkers();
        state.providers.forEach(addOrMoveMarker);
        applySort();
        renderCards();
        setResultsCount(`${state.providers.length} provider${state.providers.length !== 1 ? 's' : ''} found`);
      }
    }
    if (state.sort === 'distance') {
      applySort();
      renderCards();
    }
    if (mapInstance && Object.keys(mapMarkers).length) {
      const b = L.latLngBounds(state.center ? [state.center] : []);
      Object.values(mapMarkers).forEach((m) => b.extend(m.getLatLng()));
      if (b.isValid()) mapInstance.fitBounds(b, { padding: [50, 50], maxZoom: 14 });
    }
  }
}

/* ════════════════════════════════════════════
   SORT
   ════════════════════════════════════════════ */
function applySort() {
  const s = state.sort;
  if (s === 'name') state.providers.sort((a, b) => a.name.localeCompare(b.name));
  else if (s === 'distance')
    state.providers.sort((a, b) => {
      if (a.distance == null && b.distance == null) return 0;
      if (a.distance == null) return 1;
      if (b.distance == null) return -1;
      return a.distance - b.distance;
    });
  // 'relevance' keeps the registry's own ordering (no resort)
}

/* ════════════════════════════════════════════
   RENDER CARDS
   ════════════════════════════════════════════ */
function renderCards() {
  const list = byId('results-list');
  list.innerHTML = '';
  let items;
  if (state.activeTab === 'favorites') {
    items = Object.values(state.favorites);
    if (!items.length) {
      list.classList.remove('cards');
      list.appendChild(buildFavEmpty());
      updateResultsBar();
      return;
    }
  } else {
    if (!state.providers.length) {
      list.classList.remove('cards');
      list.appendChild(buildWelcome());
      updateResultsBar();
      return;
    }
    items = state.providers;
  }
  list.classList.add('cards');
  items.forEach((doc, i) => list.appendChild(buildCard(doc, i)));
  updateResultsBar();
}

function buildCard(doc, idx) {
  const isFav = !!state.favorites[doc.npi];
  const isActive = state.activeNpi === doc.npi;
  const card = document.createElement('div');
  card.className = `provider-card card-enter ${isActive ? 'active' : ''}`;
  card.style.animationDelay = `${Math.min(idx * 26, 200)}ms`;
  card.style.setProperty('--accent', doc.color);
  card.dataset.npi = doc.npi;
  card.dataset.action = 'open-detail';
  card.innerHTML = `
    <button class="save-btn ${isFav ? 'saved' : ''}" data-action="toggle-fav" data-npi="${esc(doc.npi)}" aria-label="${isFav ? 'Remove from saved' : 'Save provider'}" title="${isFav ? 'Remove from saved' : 'Save provider'}">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
    </button>
    <div class="card-head">
      <div class="avatar" style="background:${doc.color};">${esc(doc.initials || '?')}</div>
      <div class="card-body">
        <div class="card-topline"><button type="button" class="card-name" data-action="open-detail" data-npi="${esc(doc.npi)}" title="${esc(doc.name)}" aria-label="View details for ${esc(doc.name)}">${esc(doc.name)}</button><span class="type-chip">${doc.isOrg ? 'Org' : 'Indiv'}</span></div>
        <p class="card-specialty" title="${esc(doc.specialty)}">${esc(doc.specialty)}</p>
        ${doc.fullAddress ? `<p class="card-meta"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>${esc(doc.fullAddress)}</p>` : `<p class="card-meta none">No practice address on file</p>`}
        ${doc.phone ? `<p class="card-meta"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3-8.6A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.7a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.4-1.1a2 2 0 0 1 2.1-.5c.9.3 1.8.5 2.7.6a2 2 0 0 1 1.7 2Z"/></svg>${esc(doc.phone)}</p>` : ''}
        <div class="card-foot">
          <span class="badges">
            <span class="reg-flag"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/></svg>Official record</span>
            ${insuranceBadgesHtml(doc)}
            ${geoFlagHtml(doc)}
          </span>
          ${doc.distance != null ? `<span class="dist-badge">${doc.distance.toFixed(1)} mi</span>` : `<span class="npi-label">NPI <span class="mono">${esc(doc.npi)}</span></span>`}
        </div>
      </div>
    </div>`;
  return card;
}
function insuranceBadgesHtml(doc) {
  const ins = doc.insurance || {};
  const chk =
    '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  const out = [];
  for (const id of Object.keys(ins)) {
    const info = ins[id];
    if (!info || info.value !== true) continue;
    if (info.confidence === 'verified') {
      // Staleness demotion: a verified hit past its freshness SLO stays a real "yes" but
      // is muted and dated ("· as of <date>"), never shown as a fresh green.
      const dated = info.stale && info.fetched_at ? ` · as of ${esc(fmtDate(info.fetched_at))}` : '';
      const staleCls = info.stale ? ' stale' : '';
      if ((info.level || 'payer') === 'plan') {
        out.push(`<span class="ins-badge${staleCls}">${chk}${esc(planLabel(id))}${dated}</span>`); // green "Confirmed" — a specific program
      } else {
        out.push(`<span class="ins-badge innet${staleCls}">${esc(planLabel(id))} · in-network${dated}</span>`); // payer network listing, not plan acceptance
      }
    }
    // No estimated badges: only verified answers earn a badge. The estimated tier was
    // removed — an unverifiable "likely" is not shown as if it were provider-specific.
  }
  return out.join('');
}
function coverageHtml(doc, insSearch) {
  const ins = doc.insurance || {};
  // Show every verified result. The estimated tier was removed, so an unconfirmed catalog
  // guess never appears in the coverage list.
  const ids = Object.keys(ins).filter((id) => {
    const info = ins[id];
    return info && info.confidence === 'verified';
  });
  if (HAS_BACKEND && ids.length) {
    ids.sort((a, b) => {
      const ca = ins[a].confidence === 'verified' ? 0 : 1,
        cb = ins[b].confidence === 'verified' ? 0 : 1;
      return ca - cb || planLabel(a).localeCompare(planLabel(b));
    });
    const items = ids
      .map((id) => {
        const info = ins[id];
        const pm = planMeta(id);
        const s = coverageStatus(info, pm ? pm.filterable : undefined);
        const status = `<span class="cov-status ${s.cls}">${esc(s.text)}</span>`;
        // Provenance: a verified answer always carries a source URL + fetch date, so
        // the patient can verify it at the real source (the A3 trust rule).
        const verify =
          info.confidence === 'verified' && info.source_url
            ? `<a class="cov-verify" href="${esc(info.source_url)}" target="_blank" rel="noopener">verify${info.fetched_at ? ` · checked ${esc(fmtDate(info.fetched_at))}` : ''}</a>`
            : '';
        return `<div class="cov-item"><span>${esc(planLabel(id))}</span><span class="cov-right">${status}${verify}</span></div>`;
      })
      .join('');
    return `<div class="cov-list">${items}</div>
      <p style="font-size:.7rem;color:var(--faint);margin:9px 0 0;line-height:1.5;"><b>Confirmed</b> = enrolled in a specific program (e.g. official Medicare). <b>In-network</b> = listed in this payer's network directory — confirm your specific plan. <a href="${insSearch}" target="_blank" rel="noopener">Confirm directly</a>.</p>`;
  }
  return `<div class="coverage-note">
      <b>Insurance networks are not part of the public registry.</b> This standalone build shows the official CMS record, which does not list accepted plans. Connect the InNetwork backend to enable verified Medicare and payer-network filtering. Until then, confirm coverage directly before booking.
      <div class="cov-actions">${doc.phone ? `<a href="tel:${esc(doc.phoneRaw)}">Call to confirm</a>` : ''}<a href="${insSearch}" target="_blank" rel="noopener">Search accepted plans</a></div>
    </div>`;
}
function geoFlagHtml(doc) {
  return doc.geocoded
    ? `<span class="geo-flag mapped"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>Mapped</span>`
    : `<span class="geo-flag locating"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Locating…</span>`;
}
function refreshCardGeo(doc) {
  const card = document.querySelector(`.provider-card[data-npi="${cssEsc(doc.npi)}"]`);
  if (!card) return;
  const flag = card.querySelector('.geo-flag');
  if (flag) flag.outerHTML = geoFlagHtml(doc);
  const foot = card.querySelector('.card-foot');
  if (foot && doc.distance != null) {
    const right = foot.lastElementChild;
    if (right && !right.classList.contains('dist-badge'))
      right.outerHTML = `<span class="dist-badge">${doc.distance.toFixed(1)} mi</span>`;
  }
}

function buildWelcome() {
  const d = document.createElement('div');
  d.className = 'state-block';
  const pin =
    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>';
  const examples = [
    { zip: '10001', spec: 'Cardiology', label: 'Cardiology · NYC' },
    { zip: '90210', spec: 'Dermatology', label: 'Dermatology · Beverly Hills' },
    { zip: '60601', spec: 'Family Medicine', label: 'Primary care · Chicago' },
    { zip: '33139', spec: 'Pediatrics', label: 'Pediatrics · Miami' },
  ];
  const chips = examples
    .map(
      (e) =>
        `<button class="quick-chip" type="button" data-action="quick-search" data-zip="${e.zip}" data-spec="${esc(e.spec)}">${pin}${esc(e.label)}</button>`,
    )
    .join('');
  d.innerHTML = `<svg class="state-art" width="58" height="58" viewBox="0 0 58 58" fill="none"><path d="M29 5C19.6 5 12 12.2 12 21.4 12 33.9 29 53 29 53s17-19.1 17-31.6C46 12.2 38.4 5 29 5Z" fill="#dcefe8" stroke="#0f7a5f" stroke-width="2"/><path d="M29 14v15M21.5 21.5h15" stroke="#0f7a5f" stroke-width="2.6" stroke-linecap="round"/></svg>
    <p class="state-title">Search the national registry</p>
    <p class="state-text">Enter a ZIP code, a city and state, or an NPI to find licensed providers from the official CMS NPPES database — 8 million+ real records, no login.</p>
    <p class="quick-label">Try a sample search</p>
    <div class="quick-chips">${chips}</div>`;
  return d;
}
function buildFavEmpty() {
  const d = document.createElement('div');
  d.className = 'state-block';
  d.innerHTML = `<svg class="state-art" width="50" height="50" viewBox="0 0 24 24" fill="#fbf0d8" stroke="#c2861b" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
    <p class="state-title">Nothing saved yet</p>
    <p class="state-text">Tap the bookmark on any provider to keep them here for later. Saved providers can be exported as CSV or JSON.</p>`;
  return d;
}
function showEmptyState(msg) {
  const list = byId('results-list');
  list.classList.remove('cards');
  const hasFilter = state.selectedPlans.length > 0;
  const tip = hasFilter
    ? `Your insurance filter may be too narrow — try clearing it.`
    : `Try a nearby ZIP, a broader specialty, or a higher result limit.`;
  const clearBtn = hasFilter
    ? `<button class="state-retry" data-action="clear-plans" style="margin-top:8px;">Clear insurance filter &amp; search again</button>`
    : '';
  list.innerHTML = `<div class="state-block"><svg class="state-art" width="52" height="52" viewBox="0 0 24 24" fill="none" stroke="#8a958f" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
    <p class="state-title">No providers found</p>
    <p class="state-text">${esc(msg)} ${tip}</p>${clearBtn}</div>`;
  byId('results-count-header').textContent = '';
  updateResultsBar();
}
function showErrorState(msg) {
  const list = byId('results-list');
  list.classList.remove('cards');
  list.innerHTML = `<div class="state-block"><svg class="state-art" width="50" height="50" viewBox="0 0 24 24" fill="none" stroke="#b3402f" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></svg>
    <p class="state-title">Couldn't reach the registry</p>
    <p class="state-text err">${esc(msg)}</p>
    <button class="state-retry" data-action="search">Try again</button></div>`;
  byId('results-count-header').textContent = '';
  updateResultsBar();
}
// Honest state when the backend isn't running and there's no working standalone path
// (the CMS registry can't be called directly from a browser). Tell the user exactly
// what to do instead of advertising a search that can't run.
function showBackendRequired() {
  const list = byId('results-list');
  list.classList.remove('cards');
  list.innerHTML = `<div class="state-block"><svg class="state-art" width="50" height="50" viewBox="0 0 24 24" fill="none" stroke="#b3402f" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M7 20h10M9 16v4M15 16v4"/></svg>
    <p class="state-title">Start the InNetwork backend</p>
    <p class="state-text">Provider search runs through the InNetwork backend — the public CMS registry can't be queried directly from a browser. Start it and reload:</p>
    <p class="state-text" style="font-family:ui-monospace,Menlo,monospace;background:var(--card,#f4f6f5);padding:8px 12px;border-radius:8px;">uvicorn app.main:app --port 8000</p>
    <p class="state-text">then open <b>http://localhost:8000</b>. <button class="state-retry" data-action="search" style="margin-top:8px;">Try again</button></p></div>`;
  byId('results-count-header').textContent = '';
  updateResultsBar();
}
function updateResultsBar() {
  const bar = byId('results-bar');
  const txt = byId('results-bar-text');
  const sortWrap = byId('sort-wrap');
  const exportWrap = byId('export-wrap');
  if (state.activeTab === 'results' && state.providers.length) {
    bar.style.display = 'flex';
    sortWrap.style.display = 'inline-flex';
    exportWrap.style.display = 'none';
    const shown = state.providers.length;
    const m = state.searchMeta;
    // When the backend dropped results past the limit, say so plainly ("showing N
    // of M") instead of presenting the truncated slice as the whole set. A capped
    // upstream pool means M is itself a floor, shown as "M+".
    const count =
      m && m.truncated && m.total > shown
        ? `<b>${shown}</b> of ${m.total}${m.poolCapped ? '+' : ''} providers`
        : `<b>${shown}</b> providers`;
    const near = state.centerLabel ? ` near ${esc(state.centerLabel)}` : '';
    const more = m && m.truncated ? ` <span class="results-hint">· refine your search to see more</span>` : '';
    txt.innerHTML = `${count}${near}${more}`;
  } else if (state.activeTab === 'favorites' && Object.keys(state.favorites).length) {
    bar.style.display = 'flex';
    sortWrap.style.display = 'none';
    exportWrap.style.display = 'inline-flex';
    txt.innerHTML = `<b>${Object.keys(state.favorites).length}</b> saved`;
  } else {
    bar.style.display = 'none';
  }
}

/* ════════════════════════════════════════════
   DETAIL DRAWER
   ════════════════════════════════════════════ */
let lastFocus = null;
function openDetail(npi) {
  const doc = state.providers.find((d) => d.npi === npi) || state.favorites[npi];
  if (!doc) return;
  selectDoctor(npi, true);
  lastFocus = document.activeElement;
  byId('drawer-avatar').textContent = doc.initials || '?';
  byId('drawer-avatar').style.background = doc.color;
  byId('drawer-name').textContent = doc.name;
  byId('drawer-spec').textContent = doc.specialty;
  byId('drawer-body').innerHTML = buildDetail(doc);
  byId('scrim').classList.add('open');
  const dr = byId('detail-drawer');
  dr.classList.add('open');
  dr.focus();
  document.addEventListener('keydown', onDrawerKey);
}
function closeDetail() {
  byId('scrim').classList.remove('open');
  byId('detail-drawer').classList.remove('open');
  document.removeEventListener('keydown', onDrawerKey);
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}
function onDrawerKey(e) {
  if (e.key === 'Escape') {
    closeDetail();
    return;
  }
  trapTab(e, byId('detail-drawer'));
}

/* Keep keyboard focus inside an open dialog (drawer/modal) — accessibility. */
function focusables(container) {
  return [
    ...container.querySelectorAll(
      'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea,[tabindex]:not([tabindex="-1"])',
    ),
  ].filter((el) => el.offsetParent !== null);
}
function trapTab(e, container) {
  if (e.key !== 'Tab' || !container) return;
  const f = focusables(container);
  if (!f.length) return;
  const first = f[0],
    last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function buildDetail(doc) {
  const isFav = !!state.favorites[doc.npi];
  const dir = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(doc.fullAddress || doc.name)}`;
  const record = `https://npiregistry.cms.hhs.gov/provider-view/${encodeURIComponent(doc.npi)}`;
  const insSearch = `https://www.google.com/search?q=${encodeURIComponent(`${doc.name} ${doc.city} ${doc.stateAb} accepted insurance`)}`;
  const taxes = doc.taxonomies
    .map(
      (t) =>
        `<div class="tax-item"><div class="tax-desc">${esc(t.desc)}${t.primary ? '<span class="primary-tag">Primary</span>' : ''}</div><div class="tax-meta">${[t.code ? 'Taxonomy ' + esc(t.code) : '', t.license ? 'License ' + esc(t.license) + (t.state ? ' (' + esc(t.state) + ')' : '') : ''].filter(Boolean).join(' · ') || 'No license on file'}</div></div>`,
    )
    .join('');
  const row = (k, v) => (v ? `<div class="detail-row"><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>` : '');
  return `
    <div class="action-row">
      ${doc.phone ? `<a class="action-btn primary" href="tel:${esc(doc.phoneRaw)}"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3-8.6A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.7a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.4-1.1a2 2 0 0 1 2.1-.5c.9.3 1.8.5 2.7.6a2 2 0 0 1 1.7 2Z"/></svg>Call ${esc(doc.phone)}</a>` : `<button class="action-btn" disabled style="opacity:.5;">No phone on file</button>`}
      <a class="action-btn" href="${dir}" target="_blank" rel="noopener"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>Directions</a>
      <button class="action-btn" data-action="toggle-fav" data-npi="${esc(doc.npi)}"><svg width="15" height="15" viewBox="0 0 24 24" fill="${isFav ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>${isFav ? 'Saved' : 'Save'}</button>
      <button class="action-btn" data-action="share-npi" data-npi="${esc(doc.npi)}"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4M15.4 6.5 8.6 10.5"/></svg>Share</button>
    </div>

    <div class="detail-section">
      <h3>Coverage</h3>
      ${coverageHtml(doc, insSearch)}
    </div>

    <div class="detail-section">
      <h3>Practice details</h3>
      <dl>
        ${row('Type', doc.isOrg ? 'Organization' : 'Individual')}
        ${row('Practice address', doc.fullAddress)}
        ${row('Mailing address', doc.mailingAddress && doc.mailingAddress !== doc.fullAddress ? doc.mailingAddress : '')}
        ${row('Phone', doc.phone)}
        ${row('Fax', doc.fax)}
        ${row('Gender', doc.gender)}
        ${row('Sole proprietor', doc.soleProprietor === 'YES' ? 'Yes' : doc.soleProprietor === 'NO' ? 'No' : '')}
        ${doc.distance != null ? row('Distance', `${doc.distance.toFixed(1)} miles from ${state.centerLabel || 'search center'}`) : ''}
      </dl>
    </div>

    <div class="detail-section">
      <h3>Specialties &amp; licensure</h3>
      ${taxes || '<p class="state-text" style="text-align:left;">No taxonomy on file.</p>'}
    </div>

    <div class="detail-section">
      <h3>Registry record</h3>
      <dl>
        ${row('NPI', doc.npi)}
        ${row('Status', doc.status)}
        ${row('First enumerated', formatDate(doc.enumerationDate))}
        ${row('Last updated', formatDate(doc.lastUpdated))}
      </dl>
      <a class="action-btn" href="${record}" target="_blank" rel="noopener" style="width:100%; margin-top:6px;"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6M10 14 21 3"/></svg>View official CMS record</a>
    </div>

    ${CLAIM_ENABLED ? `<p class="claim-line">Are you this provider? <button data-action="open-claim" data-npi="${esc(doc.npi)}">Claim this listing</button></p>` : ''}`;
}

/* ════════════════════════════════════════════
   SELECT / HIGHLIGHT
   ════════════════════════════════════════════ */
function selectDoctor(npi, _fromCard) {
  state.activeNpi = npi;
  highlightCard(npi);
  updateMarkerStyles(npi);
  const m = mapMarkers[npi];
  if (m && mapInstance) {
    mapInstance.setView(m.getLatLng(), Math.max(mapInstance.getZoom(), 14), { animate: true });
    m.openPopup();
  }
}
function highlightCard(npi) {
  document
    .querySelectorAll('.provider-card')
    .forEach((/** @type {any} */ c) => c.classList.toggle('active', c.dataset.npi === npi));
  const a = document.querySelector(`.provider-card[data-npi="${cssEsc(npi)}"]`);
  if (a) a.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* ════════════════════════════════════════════
   FAVORITES + EXPORT
   ════════════════════════════════════════════ */
function loadFavorites() {
  try {
    state.favorites = JSON.parse(localStorage.getItem(LS_KEY)) || {};
  } catch {
    state.favorites = {};
  }
}
function saveFavorites() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(state.favorites));
  } catch (_) {}
}
function toggleFavorite(npi) {
  const doc = state.providers.find((d) => d.npi === npi) || state.favorites[npi];
  if (!doc) return;
  if (state.favorites[npi]) {
    delete state.favorites[npi];
    showToast('Removed from saved');
  } else {
    state.favorites[npi] = doc;
    showToast('Saved for later');
  }
  saveFavorites();
  updateFavBadge();
  renderCards();
  if (byId('detail-drawer').classList.contains('open')) openDetail(npi);
}
function updateFavBadge() {
  const c = Object.keys(state.favorites).length;
  const b = byId('fav-count-badge');
  b.textContent = c;
  b.style.display = c > 0 ? 'inline-flex' : 'none';
}
function exportFavorites(kind) {
  const items = Object.values(state.favorites);
  if (!items.length) {
    showToast('No saved providers to export.');
    return;
  }
  let blob, fname;
  if (kind === 'json') {
    blob = new Blob([JSON.stringify(items, null, 2)], { type: 'application/json' });
    fname = 'innetwork-saved.json';
  } else {
    const cols = ['npi', 'name', 'specialty', 'fullAddress', 'phone', 'fax', 'gender', 'status'];
    // csvCell quotes AND neutralizes spreadsheet formula injection (shared, unit-tested).
    const rows = [cols.join(',')].concat(items.map((d) => cols.map((c) => csvCell(d[c])).join(',')));
    blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    fname = 'innetwork-saved.csv';
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fname;
  a.click();
  URL.revokeObjectURL(url);
  showToast(`Exported ${items.length} provider${items.length !== 1 ? 's' : ''}`);
}

/* ════════════════════════════════════════════
   TABS / VIEW
   ════════════════════════════════════════════ */
function switchTab(tab) {
  withVT(() => {
    state.activeTab = tab;
    byId('tab-results').classList.toggle('active', tab === 'results');
    byId('tab-favorites').classList.toggle('active', tab === 'favorites');
    renderCards();
  });
}
function switchTabSilent(tab) {
  state.activeTab = tab;
  byId('tab-results').classList.toggle('active', tab === 'results');
  byId('tab-favorites').classList.toggle('active', tab === 'favorites');
}
function setView(which) {
  withVT(() => {
    document.body.classList.toggle('show-map', which === 'map');
    document.querySelectorAll('.view-toggle button').forEach((/** @type {any} */ b) => {
      const on = b.dataset.action === `view-${which}`;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  });
  if (which === 'map' && mapInstance) setTimeout(() => mapInstance.invalidateSize(), 60);
}

/* ════════════════════════════════════════════
   URL STATE (shareable searches)
   ════════════════════════════════════════════ */
function writeUrl(f) {
  const p = new URLSearchParams();
  if (f.npi) p.set('npi', f.npi);
  if (f.zip) p.set('zip', f.zip);
  if (f.city) p.set('city', f.city);
  if (f.st) p.set('st', f.st);
  if (f.specialty) p.set('spec', f.specialty);
  if (f.type) p.set('type', f.type);
  if (f.name) p.set('name', f.name);
  if (f.radius && f.radius !== '25') p.set('r', f.radius);
  if (f.limit && f.limit !== '25') p.set('limit', f.limit);
  // Keep the insurance filter in shared links so a sent search reproduces faithfully.
  if (state.selectedPlans.length) p.set('plans', state.selectedPlans.join(','));
  const qs = p.toString();
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}
function readUrl() {
  const p = new URLSearchParams(location.search);
  if (![...p.keys()].length) return false;
  const set = (id, v) => {
    if (v != null) {
      const el = byId(id);
      if (el) el.value = v;
    }
  };
  set('npi-input', p.get('npi'));
  set('zip-input', p.get('zip'));
  set('city-input', p.get('city'));
  set('state-select', p.get('st'));
  set('specialty-select', p.get('spec'));
  set('type-select', p.get('type'));
  set('name-input', p.get('name'));
  set('radius-select', p.get('r'));
  set('limit-select', p.get('limit'));
  // Restore the insurance filter from the link. renderInsuranceFilter() (called once
  // loadPlans() resolves) reads these, so the chips show checked and the mode set.
  const plans = p.get('plans');
  if (plans)
    state.selectedPlans = plans
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
  if (state.plans.length) renderInsuranceFilter();
  if (p.get('name') || p.get('city') || p.get('st') || p.get('npi') || p.get('type')) openAdv(true);
  return true;
}

/* ════════════════════════════════════════════
   CLAIM MODAL
   ════════════════════════════════════════════ */
let claimLastFocus = null;
function openClaim(npi) {
  const subject = encodeURIComponent(npi ? `Claim InNetwork listing — NPI ${npi}` : 'Claim a InNetwork listing');
  byId('claim-link').href = `mailto:${CLAIM_EMAIL}?subject=${subject}`;
  const m = byId('claim-modal');
  m.classList.add('open');
  claimLastFocus = document.activeElement;
  byId('claim-link').focus();
  document.addEventListener('keydown', onClaimKey);
}
function closeClaim() {
  byId('claim-modal').classList.remove('open');
  document.removeEventListener('keydown', onClaimKey);
  if (claimLastFocus && claimLastFocus.focus) claimLastFocus.focus();
}
function onClaimKey(e) {
  if (e.key === 'Escape') {
    closeClaim();
    return;
  }
  trapTab(e, document.querySelector('#claim-modal .modal-card'));
}

/* ════════════════════════════════════════════
   UI HELPERS
   ════════════════════════════════════════════ */
function setSearchLoading(loading) {
  const btn = byId('search-btn');
  const txt = byId('search-btn-text');
  const icon = byId('search-icon');
  btn.disabled = loading;
  if (loading) {
    if (icon) icon.outerHTML = '<div class="spinner" id="search-icon"></div>';
    txt.textContent = 'Searching…';
    showSkeletons(parseInt(byId('limit-select').value, 10) > 25 ? 8 : 5);
  } else {
    const sp = byId('search-icon');
    if (sp)
      sp.outerHTML =
        '<svg id="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>';
    txt.textContent = 'Search providers';
  }
}
function showSkeletons(n) {
  const list = byId('results-list');
  list.innerHTML = '';
  list.classList.add('cards');
  byId('results-bar').style.display = 'none';
  for (let i = 0; i < n; i++) {
    const el = document.createElement('div');
    el.className = 'provider-card';
    el.style.pointerEvents = 'none';
    el.innerHTML = `<div class="card-head"><div class="skeleton" style="width:42px;height:42px;border-radius:11px;flex-shrink:0;"></div><div style="flex:1;"><div class="skeleton" style="height:13px;width:62%;margin-bottom:9px;"></div><div class="skeleton" style="height:10px;width:42%;margin-bottom:10px;"></div><div class="skeleton" style="height:10px;width:82%;margin-bottom:10px;"></div><div class="skeleton" style="height:14px;width:46%;"></div></div></div>`;
    list.appendChild(el);
  }
}
function showPill(msg, dur = 3000) {
  const p = byId('map-info-pill');
  p.textContent = msg;
  p.classList.add('visible');
  clearTimeout(p._t);
  p._t = setTimeout(() => p.classList.remove('visible'), dur);
}
let toastTimer = null;
function showToast(msg) {
  const t = byId('toast');
  t.textContent = msg;
  t.style.opacity = '1';
  t.style.transform = 'translateX(-50%) translateY(0)';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    t.style.opacity = '0';
    t.style.transform = 'translateX(-50%) translateY(8px)';
  }, 2300);
}
function shake(el) {
  // Honor reduced-motion: skip the shake entirely (the toast still conveys the error).
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  el.style.animation = 'none';
  void el.offsetHeight;
  el.style.animation = 'shake .35s ease';
  el.addEventListener(
    'animationend',
    () => {
      el.style.animation = '';
    },
    { once: true },
  );
}
function openAdv(open) {
  const f = byId('adv-fields'),
    t = byId('adv-toggle');
  f.classList.toggle('open', open);
  t.setAttribute('aria-expanded', String(open));
}

/* ── Geolocation ── */
async function useLocation() {
  if (!navigator.geolocation) {
    showToast('Location is not available in this browser.');
    return;
  }
  showToast('Finding your location…');
  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      const zip = await reverseGeocode(pos.coords.latitude, pos.coords.longitude);
      if (zip) {
        byId('zip-input').value = zip.substring(0, 5);
        showToast('Location set — searching');
        handleSearch();
      } else {
        showToast("Couldn't determine your ZIP. Enter it manually.");
      }
    },
    () => showToast('Location permission denied.'),
    { timeout: 10000 },
  );
}

/* ════════════════════════════════════════════
   UTILITIES
   ════════════════════════════════════════════ */
function formatDate(s) {
  if (!s) return '';
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  // NPPES dates are date-only ("YYYY-MM-DD"), parsed as UTC midnight — format in UTC too
  // so they don't slip to the previous day in western timezones (matches fmtDate).
  return d.toLocaleDateString('en-US', { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' });
}
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/* ════════════════════════════════════════════
   EVENT DELEGATION (CSP-friendly: no inline handlers)
   ════════════════════════════════════════════ */
document.addEventListener('click', (e) => {
  const tgt = /** @type {any} */ (e.target);
  const t = tgt.closest('[data-action]');
  if (!t) return;
  const action = t.dataset.action,
    npi = t.dataset.npi;
  switch (action) {
    case 'search':
      handleSearch();
      break;
    case 'quick-search': {
      const z = byId('zip-input'),
        s = byId('specialty-select');
      if (z) z.value = t.dataset.zip || '';
      if (s) s.value = t.dataset.spec || '';
      handleSearch();
      break;
    }
    case 'toggle-fav':
      e.stopPropagation();
      toggleFavorite(npi);
      break;
    case 'open-detail':
      if (!tgt.closest('.save-btn')) openDetail(t.dataset.npi);
      break;
    case 'close-detail':
      closeDetail();
      break;
    case 'tab-results':
      switchTab('results');
      break;
    case 'tab-favorites':
      switchTab('favorites');
      break;
    case 'view-list':
      setView('list');
      break;
    case 'view-map':
      setView('map');
      break;
    case 'toggle-adv':
      openAdv(byId('adv-fields').classList.contains('open') ? false : true);
      break;
    case 'toggle-plan':
      togglePlan(t.dataset.plan);
      break;
    case 'clear-plans':
      clearPlans();
      break;
    case 'clear-plan-query':
      state.planQuery = '';
      renderInsuranceFilter();
      break;
    case 'use-location':
      useLocation();
      break;
    case 'toggle-theme':
      toggleTheme();
      break;
    case 'retry-map':
      retryMap();
      break;
    case 'share-npi':
      shareNpi(npi);
      break;
    case 'open-claim':
      closeDetail();
      openClaim(npi);
      break;
    case 'close-claim':
      closeClaim();
      break;
    case 'export-json':
      exportFavorites('json');
      break;
    case 'export-csv':
      exportFavorites('csv');
      break;
  }
});
async function shareNpi(npi) {
  const url = `${location.origin}${location.pathname}?npi=${encodeURIComponent(npi)}`;
  try {
    await navigator.clipboard.writeText(url);
    showToast('Provider link copied');
  } catch {
    showToast(url);
  }
}

/* ════════════════════════════════════════════
   BOOTSTRAP
   ════════════════════════════════════════════ */
async function bootstrap() {
  // populate state dropdown
  const st = byId('state-select');
  US_STATES.forEach((s) => {
    const o = document.createElement('option');
    o.value = s;
    o.textContent = s;
    st.appendChild(o);
  });

  // Sync the address-bar theme-color with the theme the pre-paint init chose (the
  // <head> script only sets data-theme; the meta + map tiles follow here).
  applyTheme(currentTheme());

  loadFavorites();
  loadGeocache();
  updateFavBadge();
  renderCards();
  loadPlans();

  // Reveal the "For providers" claim CTA only when a real inbox is configured;
  // otherwise it stays hidden (no dead mailto). See CLAIM_ENABLED.
  if (CLAIM_ENABLED) {
    const cta = byId('provider-cta');
    if (cta) cta.hidden = false;
  }

  byId('zip-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleSearch();
  });
  byId('zip-input').addEventListener('input', (e) => {
    e.target.value = e.target.value.replace(/\D/g, '').slice(0, 5);
  });
  byId('npi-input').addEventListener('input', (e) => {
    e.target.value = e.target.value.replace(/\D/g, '').slice(0, 10);
  });
  byId('name-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleSearch();
  });
  byId('sort-select').addEventListener('change', (e) => {
    state.sort = e.target.value;
    applySort();
    renderCards();
  });

  await leafletReady;
  if (leafletOk) initMap();
  if (!mapInstance) showMapUnavailable();

  // hydrate from a shared URL and auto-run; otherwise probe the backend so we can
  // flag upfront when search won't work because it isn't running.
  if (readUrl()) handleSearch();
  else probeBackend();
}
document.addEventListener('DOMContentLoaded', bootstrap);
