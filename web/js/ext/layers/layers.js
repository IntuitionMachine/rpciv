// Static map layers for the map art, as factories over a model (model.js) and a switch state, so the app
// (index.js) and the art gallery (web/dev/gallery.js) draw exactly the same thing.
//
//   makeLayers(model, on, {tips}) → [layer, …]   on = {roads, rail, hubs, military, wonders, trade} (booleans, read live)
//
// z slots (render/map.js Z): roads 55 and rail 56 (above rivers and borders, under city tiles), protected land 57 and
// restricted ground 58 (hatching on hexes), ports & airports 65, forts 66, wonders 67 (above city tiles, under the fog),
// name plates 68 (forts and wonders laid out together, clear of city banners and City View district plaques), trade
// routes 85 (above resources, under state names). Everything is drawn into the cached static map; nothing animates.
import { drawRoads, drawRail, drawPort, drawAirport, drawTradeRoutes, hitRoute, hitNetwork, ROAD_MIN_RPX } from "../../render/art/improvements.js";
import { drawFort, drawRestricted, drawProtected, drawWonder, drawPlaque } from "../../render/art/sites.js";
import { visibleSites, siteSize, hitSites } from "./model.js";
import { esc, int } from "../../format.js";

const inVp = (it, vp, padKm) => it.x > vp.x0 - padKm && it.x < vp.x1 + padKm && it.y > vp.y0 - padKm && it.y < vp.y1 + padKm;
const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

const GEO_SERIF = '"Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif';
const GEO_SANS = '-apple-system, "Segoe UI", system-ui, sans-serif';
const GEO_ORDER = { sea: 0, range: 1, peak: 2, lake: 3, forest: 4, plain: 5, river: 6 };

function featureVisible(f, Rpx) {
  const t = f.tier || 1;
  switch (f.kind) {
    case "sea": return t === 1 ? Rpx >= 0.4 : Rpx >= 1.5;
    case "range": return t === 1 ? Rpx >= 1.2 : Rpx >= 3.0;
    case "peak": return Rpx >= 2.5;
    case "lake": return t === 1 ? Rpx >= 2.2 : Rpx >= 4.5;
    case "forest": return Rpx >= 3.0;
    case "plain": return Rpx >= 3.0;
    case "river": return Rpx >= 4.0;
    default: return Rpx >= 3.0;
  }
}

/** Normalize an angle in degrees so text is never drawn upside-down. */
function uprightRad(deg) {
  let a = ((deg || 0) % 360 + 360) % 360;
  if (a > 90 && a < 270) a -= 180;
  return (a * Math.PI) / 180;
}

/** Measure / draw centered text with extra letter-spacing (tracking in css px). */
function measureSpaced(ctx, text, tracking) {
  if (!tracking || text.length <= 1) return ctx.measureText(text).width;
  let w = 0;
  for (let i = 0; i < text.length; i++) w += ctx.measureText(text[i]).width;
  return w + tracking * (text.length - 1);
}

function drawSpaced(ctx, text, x, y, tracking, fill, stroke, strokeW) {
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  ctx.miterLimit = 2;
  ctx.lineWidth = strokeW;
  ctx.strokeStyle = stroke;
  ctx.fillStyle = fill;
  if (!tracking || text.length <= 1) {
    ctx.textAlign = "center";
    ctx.strokeText(text, x, y);
    ctx.fillText(text, x, y);
    return;
  }
  const total = measureSpaced(ctx, text, tracking);
  let cx = x - total / 2;
  ctx.textAlign = "left";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    ctx.strokeText(ch, cx, y);
    ctx.fillText(ch, cx, y);
    cx += ctx.measureText(ch).width + tracking;
  }
}

/** Draw a list of sites with their painter (world transform, sizes from css px). */
function drawSites(ctx, vp, o, list, paint) {
  const s = vp.s;
  const pad = 90 / s;
  for (const it of list) {
    if (!inVp(it, vp, pad)) continue;
    const size = siteSize(it, o.Rpx) / s;
    if (it.dim) ctx.globalAlpha = 0.5;
    paint(it, size);
    ctx.globalAlpha = 1;
  }
}

export function makeLayers(model, on, { tips = {}, app = null } = {}) {
  const m = model;
  // A site moved off its city tile says so under its tooltip (the numbers are always the true site's).
  const moved = (it, html) => {
    if (!html || !it.nudged || typeof html !== "string") return html;
    const city = m.w.cityByCell.get(it.cell);
    const where = city ? `${esc(city.name)}'s city hex` : "a city hex";
    return `${html}<br><span class="muted">Drawn on the next hex so it stays visible; its true location falls in ${where}.</span>`;
  };
  const ver = () => `v${m.version}`;
  const grid = () => m.w.grid;
  // While a lens colours the map, roads and rail fade so the data stays readable.
  const lensAlpha = (o) => (o.lens ? 0.55 : 1);

  const roads = {
    id: "roads", z: 55, minRpx: ROAD_MIN_RPX,
    visible: () => !!on.roads && !!m.net,
    sig: ver,
    draw(ctx, vp, w, o) {
      ctx.globalAlpha = lensAlpha(o);
      drawRoads(ctx, m.net, vp, o);
    },
    hit: (x, y, Rpx) => hitNetwork(m.net, "road", grid(), x, y, Rpx),
    tooltip: (it) => tips.road?.(it, m) ?? null,
  };
  const rail = {
    id: "rail", z: 56, minRpx: ROAD_MIN_RPX,
    visible: () => !!on.rail && !!m.net,
    sig: ver,
    draw(ctx, vp, w, o) {
      ctx.globalAlpha = lensAlpha(o);
      drawRail(ctx, m.net, vp, o);
    },
    hit: (x, y, Rpx) => hitNetwork(m.net, "rail", grid(), x, y, Rpx),
    tooltip: (it) => tips.rail?.(it, m) ?? null,
  };
  const protectedLand = {
    id: "protected", z: 57, minRpx: 0.8,
    visible: () => !!on.wonders && m.parks.length > 0,
    sig: ver,
    draw(ctx, vp, w, o) {
      if (o.lens) return; // a lens owns the hex colours
      drawProtected(ctx, m.parks, vp, o);
    },
  };
  const restricted = {
    id: "restricted", z: 58, minRpx: 0.8,
    visible: () => !!on.military && m.ranges.length > 0,
    sig: ver,
    draw(ctx, vp, w, o) {
      drawRestricted(ctx, m.ranges, vp, o);
    },
    hit(x, y, Rpx) {
      const cell = grid().cellAt(x, y);
      if (cell < 0) return null;
      // forts on top get their own hit first (higher z); here, the range whose hexes hold the pointer
      for (const r of m.ranges) if (r.cells.has(cell)) return { kind: "range", data: r.range };
      return null;
    },
    tooltip: (it) => tips.range?.(it.data, m) ?? null,
  };
  const hubs = {
    id: "hubs", z: 65, minRpx: 4,
    visible: () => !!on.hubs && (m.ports.length > 0 || m.airports.length > 0),
    sig: ver,
    list: (Rpx) => visibleSites(m, "hubs", [...m.ports, ...m.airports], Rpx),
    draw(ctx, vp, w, o) {
      drawSites(ctx, vp, o, hubs.list(o.Rpx), (it, size) => {
        if (it.kind === "port") drawPort(ctx, it.x, it.y, size, { tier: it.tier, type: it.data.type });
        else drawAirport(ctx, it.x, it.y, size, { tier: it.tier });
      });
    },
    warm(ctx, vp, w, o) { hubs.draw(ctx, vp, w, o); },
    hit: (x, y, Rpx) => hitSites(hubs.list(Rpx), x, y, Rpx, grid()),
    tooltip: (it) => moved(it, (it.kind === "port" ? tips.port?.(it.data, m) : tips.airport?.(it.data, m)) ?? null),
  };
  const forts = {
    id: "forts", z: 66, minRpx: 4,
    visible: () => !!on.military && m.installations.length > 0,
    sig: ver,
    list: (Rpx) => visibleSites(m, "forts", m.installations, Rpx),
    draw(ctx, vp, w, o) {
      drawSites(ctx, vp, o, forts.list(o.Rpx), (it, size) => {
        drawFort(ctx, it.fort, it.x, it.y, size, { tier: it.tier, joint: !!it.data.joint, muted: !!it.muted, coastal: it.coastal, airfield: !!it.airfield });
      });
    },
    warm(ctx, vp, w, o) { forts.draw(ctx, vp, w, o); },
    hit: (x, y, Rpx) => hitSites(forts.list(Rpx), x, y, Rpx, grid()),
    tooltip: (it) => moved(it, tips.fort?.(it.data, m) ?? null),
  };
  const wonders = {
    id: "wonders", z: 67, minRpx: 0.8,
    visible: () => !!on.wonders && m.wonders.length > 0,
    sig: ver,
    list: (Rpx) => visibleSites(m, "wonders", m.wonders, Rpx),
    draw(ctx, vp, w, o) {
      drawSites(ctx, vp, o, wonders.list(o.Rpx), (it, size) => {
        drawWonder(ctx, it.wkind, it.x, it.y, size, { variant: it.variant, motif: it.motif, rank: it.data.rank });
      });
    },
    warm(ctx, vp, w, o) { wonders.draw(ctx, vp, w, o); },
    hit: (x, y, Rpx) => hitSites(wonders.list(Rpx), x, y, Rpx, grid()),
    tooltip: (it) => moved(it, tips.wonder?.(it.data, m) ?? null),
  };
  const trade = {
    id: "trade", z: 85, minRpx: 0.6,
    visible: () => !!on.trade && m.routes.length > 0,
    sig: ver,
    draw(ctx, vp, w, o) {
      drawTradeRoutes(ctx, m.routes, vp, o);
    },
    hit: (x, y, Rpx) => {
      const rt = hitRoute(m.routes, x, y, Rpx, Rpx / grid().R);
      return rt ? { kind: "route", data: rt } : null;
    },
    tooltip: (it) => tips.route?.(it.data, m) ?? null,
  };

  // Cartographic geographic feature labels (seas, mountain ranges, peaks, lakes, forests, plains, rivers).
  // Drawn at z: 88 right below state/region labels (Z.LABELS = 90) in screen space with subtle halos and collision avoidance.
  let sortedFeatures = null;
  let lastFeaturesRef = null;
  const geoList = () => {
    const raw = m.w.raw.features;
    if (!Array.isArray(raw) || !raw.length) return [];
    if (raw !== lastFeaturesRef) {
      lastFeaturesRef = raw;
      sortedFeatures = [...raw].sort((a, b) => ((a.tier || 1) - (b.tier || 1)) || ((GEO_ORDER[a.kind] ?? 9) - (GEO_ORDER[b.kind] ?? 9)));
    }
    return sortedFeatures;
  };
  const geography = {
    id: "geography", z: 88, minRpx: 0.4,
    visible: () => !!on.geography && geoList().length > 0,
    sig: () => (on.geography ? "1" : "0"),
    draw(ctx, vp, w, o) {
      const list = geoList();
      if (!list.length) return;
      const Rpx = o.Rpx;
      const padKm = 160 / vp.s;
      o.screen();
      ctx.save();
      if (o.lens) ctx.globalAlpha = 0.72;
      const placed = [];
      const overlaps = (b) => placed.some((p) => b[0] < p[2] && p[0] < b[2] && b[1] < p[3] && p[1] < b[3]);

      for (const f of list) {
        if (!featureVisible(f, Rpx) || !inVp(f, vp, padKm)) continue;
        const [sx, sy] = o.toScreen(f.x, f.y);
        const tier = f.tier || 1;
        const k = f.kind;

        if (k === "peak") {
          const r = clamp(3.2 + Rpx * 0.08, 3.4, 5.5);
          const sz = clamp(9.5 + Rpx * 0.24, 10, 13);
          ctx.font = `italic 600 ${sz}px ${GEO_SERIF}`;
          const tw = ctx.measureText(f.name).width;
          const showElev = Rpx >= 7 && f.elev_m != null && Number.isFinite(f.elev_m);
          const bh = showElev ? sz * 2.15 : sz * 1.25;
          const box = [sx - r - 2, sy - bh * 0.55, sx + r + 6 + tw, sy + bh * 0.65];
          if (overlaps(box)) continue;
          placed.push(box);

          // Crisp ▲ mountain peak triangle
          ctx.beginPath();
          ctx.moveTo(sx, sy - r * 1.05);
          ctx.lineTo(sx + r * 0.95, sy + r * 0.75);
          ctx.lineTo(sx - r * 0.95, sy + r * 0.75);
          ctx.closePath();
          ctx.lineWidth = 1.6;
          ctx.strokeStyle = "rgba(12, 16, 14, 0.88)";
          ctx.fillStyle = "#f0e0b6";
          ctx.stroke();
          ctx.fill();

          // Peak label to the right of the triangle
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          ctx.lineJoin = "round";
          ctx.miterLimit = 2;
          const lx = sx + r + 4.5;
          const ly = showElev ? sy - sz * 0.32 : sy + 0.5;
          ctx.lineWidth = 2.8;
          ctx.strokeStyle = "rgba(12, 16, 14, 0.85)";
          ctx.fillStyle = "#f4ebd0";
          ctx.strokeText(f.name, lx, ly);
          ctx.fillText(f.name, lx, ly);

          if (showElev) {
            const elevTxt = `${int(f.elev_m)} m`;
            ctx.font = `500 ${Math.max(8.5, sz - 2)}px ${GEO_SANS}`;
            ctx.lineWidth = 2.4;
            ctx.strokeStyle = "rgba(12, 16, 14, 0.82)";
            ctx.fillStyle = "rgba(226, 210, 172, 0.88)";
            ctx.strokeText(elevTxt, lx, sy + sz * 0.62);
            ctx.fillText(elevTxt, lx, sy + sz * 0.62);
          }
          continue;
        }

        let text = f.name;
        let sz, tracking = 0, fill, stroke, strokeW = 2.8, rot = 0, fontWeight = "600";
        if (k === "sea") {
          text = f.name.toUpperCase();
          sz = tier === 1 ? clamp(10 + Rpx * 1.1, 10.5, 16) : clamp(9 + Rpx * 0.65, 9.5, 13);
          tracking = tier === 1 ? 3.2 : 1.8;
          fill = tier === 1 ? "rgba(165, 208, 238, 0.60)" : "rgba(165, 208, 238, 0.54)";
          stroke = "rgba(6, 20, 36, 0.55)";
          strokeW = 2.6;
          rot = f.angle ? uprightRad(f.angle) : 0;
        } else if (k === "range") {
          text = f.name.toUpperCase();
          sz = tier === 1 ? clamp(9.5 + Rpx * 0.6, 10, 14.5) : clamp(8.5 + Rpx * 0.42, 9, 12.5);
          tracking = tier === 1 ? 2.4 : 1.5;
          fill = "rgba(236, 220, 184, 0.88)";
          stroke = "rgba(14, 18, 14, 0.78)";
          strokeW = 3.0;
          rot = uprightRad(f.angle);
        } else if (k === "lake") {
          sz = clamp(9.5 + Rpx * 0.25, 10, 13.5);
          fill = "#bfe8ff";
          stroke = "rgba(8, 24, 38, 0.82)";
        } else if (k === "forest") {
          sz = clamp(9 + Rpx * 0.22, 9.5, 13);
          fill = "#c8e6b8";
          stroke = "rgba(10, 22, 12, 0.82)";
        } else if (k === "plain") {
          sz = clamp(9 + Rpx * 0.22, 9.5, 13);
          fill = "#e6dca8";
          stroke = "rgba(18, 18, 10, 0.80)";
        } else if (k === "river") {
          sz = clamp(9 + Rpx * 0.18, 9.5, 12.5);
          fontWeight = "500";
          fill = "rgba(202, 238, 255, 0.90)";
          stroke = "rgba(8, 24, 38, 0.80)";
          strokeW = 2.5;
          rot = uprightRad(f.angle);
        } else {
          sz = 10;
          fill = "#e6dca8";
          stroke = "rgba(12, 16, 14, 0.8)";
        }

        ctx.font = `italic ${fontWeight} ${sz}px ${GEO_SERIF}`;
        const tw = measureSpaced(ctx, text, tracking);
        // Compact axis-aligned collision box (tighter for rotated labels so ranges/rivers fit cleanly)
        const c = Math.abs(Math.cos(rot)), sn = Math.abs(Math.sin(rot));
        const hw = (tw * 0.5) * c + (sz * 0.6) * sn;
        const hh = (tw * 0.5) * sn + (sz * 0.6) * c;
        const shrink = rot ? 0.72 : 0.92;
        const box = [sx - hw * shrink, sy - hh * shrink, sx + hw * shrink, sy + hh * shrink];
        if (k !== "sea" && overlaps(box)) continue;
        if (k !== "sea") placed.push(box);

        ctx.save();
        ctx.translate(sx, sy);
        if (rot) ctx.rotate(rot);
        drawSpaced(ctx, text, 0, 0, tracking, fill, stroke, strokeW);
        ctx.restore();
      }
      ctx.restore();
      o.world();
    },
    hit(x, y, Rpx) {
      const list = geoList();
      if (!list.length) return null;
      const s = Rpx / grid().R;
      let best = null;
      let bestD2 = Infinity;
      for (const f of list) {
        if (!featureVisible(f, Rpx)) continue;
        const dx = (x - f.x) * s;
        const dy = (y - f.y) * s;
        if (f.kind === "peak") {
          // Hit triangle or label just to the right
          const rx = dx - 22;
          const hitTri = dx * dx + dy * dy <= 16 * 16;
          const hitLbl = rx >= -26 && rx <= 48 && Math.abs(dy) <= 12;
          if (!hitTri && !hitLbl) continue;
          const d2 = dx * dx + dy * dy;
          if (d2 < bestD2) { bestD2 = d2; best = f; }
        } else {
          const rot = (f.kind === "range" || f.kind === "river" || f.kind === "sea") ? uprightRad(f.angle) : 0;
          const c = Math.cos(-rot), sn = Math.sin(-rot);
          const lx = dx * c - dy * sn;
          const ly = dx * sn + dy * c;
          const hw = clamp((f.name?.length || 8) * 3.8, 28, 78);
          const hh = 11;
          if (Math.abs(lx) <= hw && Math.abs(ly) <= hh) {
            const d2 = lx * lx * 0.15 + ly * ly;
            if (d2 < bestD2) { bestD2 = d2; best = f; }
          }
        }
      }
      return best ? { kind: "feature", data: best } : null;
    },
    tooltip: (it) => tips.feature?.(it.data, m) ?? null,
  };

  // Name plates under the forts (major ones, close in) and wonders, laid out together in one pass (wonders first, by
  // rank), so no two overlap; each keeps clear of where city banners stand and, in the City View, of the hexes that
  // hold a district plaque.
  const cvKey = () => {
    const cv = app?.cityView;
    const city = cv?.city;
    if (!city) return "";
    const mm = cv.model;
    return `${city.id}:${(mm?.placed || []).length}:${(mm?.inCity || []).length}`;
  };
  const plates = {
    id: "plates", z: 68, minRpx: 30,
    visible: () => (!!on.wonders && m.wonders.length > 0) || (!!on.military && m.installations.length > 0),
    sig: () => `v${m.version}|${on.wonders ? 1 : 0}${on.military ? 1 : 0}|${cvKey()}`,
    draw(ctx, vp, w, o) {
      const list = [];
      if (on.wonders) list.push(...wonders.list(o.Rpx));
      if (on.military && o.Rpx >= 60) list.push(...forts.list(o.Rpx).filter((it) => it.tier === "major"));
      if (list.length) plaques(ctx, vp, o, list);
    },
  };

  // Screen rects (css px) where banner plates stand: a box above each sizeable city tile. Banners are drawn over the
  // static map every frame and depend on hover, so this is a conservative stand-in rather than their exact boxes.
  function bannerRects(o, vp) {
    const out = [];
    const floor = o.Rpx < 40 ? 1e5 : o.Rpx < 70 ? 4e4 : 0;
    const pad = 2 * m.w.grid.R;
    for (const [cell, city] of m.w.cityByCell) {
      if (!(city.population >= floor)) continue;
      const x = m.w.grid.cx(cell), y = m.w.grid.cy(cell);
      if (x < vp.x0 - pad || x > vp.x1 + pad || y < vp.y0 - pad || y > vp.y1 + pad) continue;
      const [sx, sy] = o.toScreen(x, y);
      const half = Math.min(110, 40 + String(city.name || "").length * 4);
      out.push([sx - half, sy - o.Rpx * 0.75 - (o.Rpx > 24 ? 52 : 30), sx + half, sy - o.Rpx * 0.35]);
    }
    return out;
  }

  function districtCells() {
    const mm = app?.cityView?.city ? app.cityView.model : null;
    const out = new Set();
    if (!mm) return out;
    for (const d of mm.placed || []) if (d && d.cell != null) out.add(d.cell);
    if ((mm.inCity || []).length && mm.city?.cell != null) out.add(mm.city.cell);
    return out;
  }

  // Name plates under the sites (screen space), skipping any that would overlap one already placed or a banner.
  function plaques(ctx, vp, o, list) {
    o.screen();
    const busy = districtCells();
    const placed = bannerRects(o, vp);
    // In the City View, the open metro's hexes carry district plaques, yield chips and site labels (drawn over the
    // map each frame), so no fort or wonder plate may stand on them: the dock lists those sites instead.
    const mm = app?.cityView?.city ? app.cityView.model : null;
    for (const t of mm?.tiles || []) {
      if (t?.x == null || t?.y == null) continue;
      const [hx, hy] = o.toScreen(t.x, t.y);
      placed.push([hx - o.Rpx * 0.95, hy - o.Rpx, hx + o.Rpx * 0.95, hy + o.Rpx * 1.05]);
    }
    const scale = Math.min(1.2, 0.85 + o.Rpx / 200);
    const g = m.w.grid;
    for (const it of list) {
      if (!inVp(it, vp, 0)) continue;
      if (busy.size && (busy.has(it.drawCell) || busy.has(g.cellAt(it.x, it.y)))) continue;
      const [sx, sy] = o.toScreen(it.x, it.y);
      const size = siteSize(it, o.Rpx);
      const text = it.data.short || it.data.name || "";
      if (!text) continue;
      const kind = it.kind === "fort" ? "fort" : it.wkind;
      const y = sy + size * 0.42;
      const box = drawPlaque(ctx, text, sx, y, { kind, scale, measureOnly: true });
      if (placed.some((p) => box[0] < p[2] && p[0] < box[2] && box[1] < p[3] && p[1] < box[3])) continue;
      placed.push(box);
      drawPlaque(ctx, text, sx, y, { kind, scale });
    }
    o.world();
  }

  return [roads, rail, protectedLand, restricted, hubs, forts, wonders, plates, trade, geography];
}
