// Map-art extension: tile improvements (Interstates and major highways, railroads, ports, airports),
// military forts and restricted ground, wonders and protected land, and freight trade routes, each as a static layer
// drawn into the cached map (nothing animates; an idle map draws no frames), with a switch in the "Map" menu.
//
// Data: infrastructure.json, military.json, wonders.json and trade.json (loaded eagerly by main.js right after the
// first frame; app.loadData returns the same promise). A missing file (null) leaves its layers off and their switches
// harmless. Defaults: roads, rail, ports & airports, military and wonders on (roads, rail and hubs only from the region
// view in: see each layer's minRpx), trade routes off.
import { createModel } from "./model.js";
import { makeLayers } from "./layers.js";
import { tips } from "./tips.js";

const TOGGLES = [
  { id: "geography", label: "Geography", icon: "g-land", def: true, title: "Seas, mountain ranges, peaks, lakes, forests, plains and rivers", layers: ["geography"] },
  { id: "roads", label: "Roads", icon: "t-road", def: true, title: "Expressways (NLEX, SLEX, TPLEX, SCTEX, STAR, CALAX, CCLEX), AH26 & primary highways", layers: ["roads"] },
  { id: "rail", label: "Railroads", icon: "t-rail", def: true, title: "PNR main line, Metro Manila LRT/MRT & regional rail corridors", layers: ["rail"] },
  { id: "hubs", label: "Ports & airports", icon: "d-harbor", def: true, title: "Philippine seaports and RoRo terminals (HDX / OSM), airports (OurAirports / CAAP)", layers: ["hubs"] },
  { id: "military", label: "Military", icon: "m-army", def: true, title: "Defense installations as forts, largest ranges as restricted ground", layers: ["forts", "restricted", "plates"] },
  { id: "wonders", label: "Wonders", icon: "w-landmark", def: true, title: "Natural parks, UNESCO heritage sites, top universities and landmarks", layers: ["wonders", "protected", "plates"] },
  { id: "trade", label: "Trade routes", icon: "t-trade", def: false, title: "Largest domestic freight flows between regions (PSA domestic trade)", layers: ["trade"] },
];

export function install(app) {
  const model = createModel(app.w);
  const on = Object.fromEntries(TOGGLES.map((t) => [t.id, t.def]));
  const layers = makeLayers(model, on, { tips, app });
  for (const l of layers) app.registerStaticLayer(l);

  for (const t of TOGGLES) {
    app.ui.addMapToggle({
      id: t.id, label: t.label, icon: t.icon, title: t.title, default: t.def,
      get: () => on[t.id],
      set: (v) => {
        on[t.id] = !!v;
        app.requestDraw();
      },
    });
  }

  const apply = (name, data) => {
    model.setData(name, data);
    app.requestDraw();
  };
  for (const name of ["infrastructure", "military", "wonders", "trade"]) {
    if (app.data[name] !== undefined) apply(name, app.data[name]);
    else app.loadData(name).then((d) => apply(name, d), (err) => console.warn(`map layers: ${name} failed to load`, err));
  }

  // For debugging and the gallery: the model and switches.
  app.mapArt = { model, on, layers };
}
