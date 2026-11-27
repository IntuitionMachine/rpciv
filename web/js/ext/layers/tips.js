// Hover tooltips for the map-art layers (HTML strings; every data value is escaped). Each says where the number comes
// from and the caveat that matters for it (FAF values are model estimates; bases have no published headcount; the
// trade arc is a drawing aid; curated landmarks are an editorial pick).
import { esc, compact, int, pct } from "../../format.js";
import { FORT_KINDS, fortKind, WONDER_KINDS } from "../../render/art/sites.js";
import { MODE_STYLE } from "../../render/art/improvements.js";

const muted = (s) => `<span class="muted">${s}</span>`;
const num = (n, d = 1) => (n == null || !Number.isFinite(n) ? "–" : compact(n));
const sign = (x) => (x > 0 ? "+" : x < 0 ? "−" : "±");

function usd(n) {
  if (n == null || !Number.isFinite(n)) return "–";
  const a = Math.abs(n);
  if (a >= 1e12) return `₱${(n / 1e12).toFixed(2)} trillion`;
  if (a >= 1e9) return `₱${(n / 1e9).toFixed(a >= 1e10 ? 1 : 2)} billion`;
  if (a >= 1e6) return `₱${(n / 1e6).toFixed(1)} million`;
  return `₱${int(n)}`;
}

const HUB_NAME = { L: "Large hub", M: "Medium hub", S: "Small hub", N: "Regional airport" };
const PORT_TYPE = { coastal: "Seaport", great_lakes: "Lake port", inland: "River port" };
const GEO_KIND = {
  sea: "Sea / Gulf",
  range: "Mountain range",
  peak: "Peak / Volcano",
  lake: "Lake",
  forest: "Forest / Wetland",
  plain: "Plain / Valley",
  river: "River",
};

export const tips = {
  port(p, m) {
    const yr = p.year ?? "";
    const lines = [
      `<b>${esc(p.name)}</b> ${muted(`${esc(PORT_TYPE[p.type] || "Port")}, ${esc(p.state || "")}`)}`,
      `#${esc(p.rank)} by tonnage · <b>${num(p.tons)}</b> metric tons${yr ? ` (${esc(yr)})` : ""}`,
    ];
    if (p.foreign != null || p.domestic != null) lines.push(`Foreign ${num(p.foreign)} · domestic ${num(p.domestic)}`);
    if (p.imports != null || p.exports != null) lines.push(`Imports ${num(p.imports)} · exports ${num(p.exports)}`);
    lines.push(muted("Philippine seaports and RoRo terminals (HDX / OpenStreetMap)."));
    return lines.join("<br>");
  },
  airport(a) {
    const lines = [`<b>${esc(a.id)} · ${esc(a.name)}</b> ${muted(esc([a.city, a.state].filter(Boolean).join(", ")))}`];
    lines.push(a.hub ? esc(HUB_NAME[a.hub] || a.hub) : "Cargo / secondary airport");
    if (a.enplanements != null) {
      let ch = "";
      if (a.enplanements_prev > 0) {
        const r = a.enplanements / a.enplanements_prev - 1;
        ch = ` (${sign(r)}${Math.abs(r * 100).toFixed(1)}% on ${a.year - 1})`;
      }
      lines.push(`<b>${num(a.enplanements)}</b> passengers in ${esc(a.year)}${ch}`);
    }
    if (a.cargo_lbs != null) lines.push(`Cargo volume ${num(a.cargo_lbs)} lb`);
    lines.push(muted("OurAirports Philippines / CAAP airport data."));
    return lines.join("<br>");
  },
  fort(f, m) {
    const comp = m.data.military?.components?.find((c) => c.id === f.component);
    const name = comp?.name || FORT_KINDS[fortKind(f.component)]?.name || "Defense site";
    const tier = { major: "Major installation", base: "Base", site: "Site" }[f.tier] || "";
    const status = { act: "", semi: " · semi-active", care: " · caretaker status" }[f.status] ?? "";
    const lines = [
      `<b>${esc(f.name)}</b> ${muted(esc(f.state || ""))}`,
      `${esc(name)}${f.joint ? " · joint base" : ""} · ${esc(tier)}${esc(status)}`,
    ];
    if (f.area_km2 != null) lines.push(`Boundary ${int(f.area_km2)} km²`);
    lines.push(muted("Defense installation data. Sized by boundary area."));
    return lines.join("<br>");
  },
  range(r, m) {
    const comp = m.data.military?.components?.find((c) => c.id === r.component);
    return [
      `<b>${esc(r.name)}</b> ${muted(esc(r.state || ""))}`,
      `Restricted ground · ${esc(comp?.name || "")} · ${int(r.area_km2)} km²`,
      muted("Military range drawn on the hexes it covers."),
    ].join("<br>");
  },
  wonder(x, m) {
    const k = WONDER_KINDS[x.kind]?.name || "Wonder";
    const rule = m.data.wonders?.rules?.find((r) => r.id === x.rule);
    const mt = x.metrics || {};
    const lines = [`<b>${esc(x.name)}</b> ${muted(esc([x.state].filter(Boolean).join("")))}`];
    if (x.kind === "university") {
      lines.push(`${esc(k)} · #${esc(x.rank)}`);
      if (mt.rd_usd != null) lines.push(`R&amp;D ${usd(mt.rd_usd)} (${esc(mt.year)})`);
      if (mt.enrollment != null) lines.push(`${int(mt.enrollment)} students (${esc(mt.enrollment_year)})`);
    } else if (x.curated) {
      lines.push(`${esc(k)} · curated heritage &amp; landmark`);
      if (mt.visits != null) lines.push(`${num(mt.visits)} visits (${esc(mt.year)})`);
      if (mt.designated) lines.push(`Inscribed / designated ${esc(mt.designated)}`);
    } else {
      lines.push(`${esc(k)} · #${esc(x.rank)}`);
      if (mt.visits != null) lines.push(`<b>${num(mt.visits)}</b> visits (${esc(mt.year)})${mt.visits_of ? ` ${muted(`(${esc(mt.visits_of)})`)}` : ""}`);
      if (mt.area_km2 != null) lines.push(`${int(mt.area_km2)} km²`);
    }
    if (rule?.text) lines.push(muted(esc(rule.text)));
    return lines.join("<br>");
  },
  route(rt, m) {
    const r = rt.route;
    const t = m.data.trade || {};
    const a = rt.a?.short || rt.a?.name || r.a, b = rt.b?.short || rt.b?.name || r.b;
    const modes = Object.entries(r.modes || {}).filter(([, v]) => v >= 0.05).sort((p, q) => q[1] - p[1])
      .map(([k, v]) => `${esc(MODE_STYLE[k]?.label || k)} ${Math.round(v * 100)}%`).join(" · ");
    const com = t.commodities?.[r.top_commodity];
    const lines = [
      `<b>${esc(a)} ⇄ ${esc(b)}</b> ${muted(`trade route #${esc(r.rank)}`)}`,
      `<b>${usd(r.value_musd * 1e6)}</b> of freight a year, ${num(r.tons_k * 1e3)} metric tons (PSA domestic trade ${esc(t.year ?? "")})`,
    ];
    if (modes) lines.push(modes);
    if (com) lines.push(`Top commodity: ${esc(com)}`);
    lines.push(muted("Both directions summed. Drawn as an arc between regional hubs: PSA domestic trade records origin and destination regions, not exact routes."));
    return lines.join("<br>");
  },
  feature(f) {
    const lines = [`<b>${esc(f.name)}</b> ${muted(esc(GEO_KIND[f.kind] || "Geographic feature"))}`];
    if (f.elev_m != null && Number.isFinite(f.elev_m)) {
      lines.push(`Elevation <b>${int(f.elev_m)} m</b> ${muted(`(${int(Math.round(f.elev_m * 3.28084))} ft)`)}`);
    }
    if (f.note) lines.push(muted(esc(f.note)));
    return lines.join("<br>");
  },
  road(r) {
    const cls = r.cls === 0 ? "Expressway / AH26 Spine" : "National primary highway";
    const title = r.name ? `<b>${esc(r.name)}</b> ${muted(cls)}` : `<b>${cls}</b>`;
    return [title, muted("Philippine expressway and trunk highway network (OpenStreetMap / DPWH).")].join("<br>");
  },
  rail(r) {
    const cls = r.cls === 0 ? "Main-line / Metro rail" : "Regional / secondary rail";
    const title = r.name ? `<b>${esc(r.name)}</b> ${muted(cls)}` : `<b>${cls}</b>`;
    return [title, muted("Philippine rail network — PNR, LRT and MRT corridors (OpenStreetMap / DOTr).")].join("<br>");
  },
  dive(d, m) {
    const t = m.diveTypes?.get(d.kind);
    const badgeColor = t?.color || "#26a69a";
    const badgeName = t?.name || d.kind || "Dive site";
    const badge = `<span style="display:inline-block;padding:0 5px;border-radius:3px;font-size:10.5px;font-weight:600;background:${esc(badgeColor)}28;border:1px solid ${esc(badgeColor)};color:${esc(badgeColor)};vertical-align:1px">${esc(badgeName)}</span>`;
    const lines = [
      `<b>${esc(d.name)}</b> ${badge}`,
      muted(esc([d.area, d.region ? `Region ${d.region}` : ""].filter(Boolean).join(" · "))),
    ];
    const meta = [];
    if (d.depth_m) meta.push(`Depth <b>${esc(d.depth_m)}</b>`);
    if (d.season) meta.push(`Season <b>${esc(d.season)}</b>`);
    if (d.level) meta.push(esc(d.level));
    if (meta.length) lines.push(meta.join(" · "));
    if (d.highlights) lines.push(esc(d.highlights));
    return lines.join("<br>");
  },
};
