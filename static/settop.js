// TV Thing -- the set-top look's LED glass (settop.css): drawSegments(svg, "8:42") draws
// text in seven-segment digits, and setMarqueeText scrolls a display line that does not fit.
// Used by the VCR page and the Scan and DX pages.

// ---------- seven-segment glass ----------
// Segments a-g on a 12x22 cell, slanted like the real thing. Unlit segments
// are drawn faintly, because on a VFD you can always see them.

const SEGMENTS = {
  a: [2.6, 0.4, 6.8, 1.8], b: [9.8, 2.4, 1.8, 7.8], c: [9.8, 11.8, 1.8, 7.8],
  d: [2.6, 19.8, 6.8, 1.8], e: [0.4, 11.8, 1.8, 7.8], f: [0.4, 2.4, 1.8, 7.8],
  g: [2.6, 10.1, 6.8, 1.8],
};
const GLYPHS = {
  "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
  "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg",
  "-": "g", " ": "", "_": "d", A: "abcefg", b: "cdefg", C: "adef", c: "deg",
  d: "bcdeg", E: "adefg", F: "aefg", H: "bcefg", h: "cefg", L: "def",
  n: "ceg", o: "cdeg", P: "abefg", r: "eg", t: "defg", U: "bcdef", u: "cde",
  y: "bcdfg", S: "afgcd", O: "abcdef",
};

function drawSegments(svg, text) {
  const ns = "http://www.w3.org/2000/svg";
  svg.replaceChildren();
  let x = 0;
  for (const ch of text) {
    const group = document.createElementNS(ns, "g");
    group.setAttribute("transform", `translate(${x} 0) skewX(-7)`);
    if (ch === ":" || ch === ".") {
      const dots = ch === ":" ? [7, 15] : [20.5];
      for (const cy of dots) {
        const dot = document.createElementNS(ns, "rect");
        Object.entries({ x: 1.4, y: cy - 0.9, width: 1.8, height: 1.8 })
          .forEach(([k, v]) => dot.setAttribute(k, v));
        dot.setAttribute("class", "on");
        group.appendChild(dot);
      }
      svg.appendChild(group);
      x += 5;
      continue;
    }
    const lit = GLYPHS[ch] ?? GLYPHS[ch.toUpperCase()] ?? GLYPHS[ch.toLowerCase()] ?? "";
    for (const [name, [sx, sy, w, h]] of Object.entries(SEGMENTS)) {
      const seg = document.createElementNS(ns, "rect");
      seg.setAttribute("x", sx); seg.setAttribute("y", sy);
      seg.setAttribute("width", w); seg.setAttribute("height", h);
      seg.setAttribute("rx", 0.8);
      seg.setAttribute("class", lit.includes(name) ? "on" : "off");
      group.appendChild(seg);
    }
    svg.appendChild(group);
    x += 14;
  }
  svg.setAttribute("viewBox", `-1 0 ${Math.max(x, 1) + 1} 22.4`);
}

// A display line: ``box`` holding ``inner``; scrolls when the text is wider than the box.
function setMarqueeText(box, inner, text) {
  if (inner.textContent === text) return;
  inner.textContent = text;
  box.classList.remove("scroll");
  requestAnimationFrame(() => {
    box.classList.toggle("scroll", inner.scrollWidth > box.clientWidth);
  });
}

// The box's clock: "8:42", as the Explorer shows it when idle.
function clockText(now = new Date()) {
  const hours = now.getHours() % 12 || 12;
  return `${String(hours).padStart(2, " ")}:${String(now.getMinutes()).padStart(2, "0")}`;
}

// One Explorer's front panel (its .settop-window): the clock, which keeps the time until a
// page holds it to show something else; the lamps (data-lamp); and the scrolling line.
function explorerPanel(root) {
  const clock = root.querySelector("svg.settop-clock");
  const line = root.querySelector(".settop-text");
  const inner = line.querySelector("span");
  let held = null;
  const tick = () => { if (held === null) drawSegments(clock, clockText()); };
  tick();
  setInterval(tick, 10000);
  return {
    clock(text) { held = text; if (text === null) tick(); else drawSegments(clock, text); },
    say(text) { setMarqueeText(line, inner, String(text).toUpperCase()); },
    lamp(name, lit) { root.querySelector(`[data-lamp="${name}"]`)?.classList.toggle("lit", Boolean(lit)); },
  };
}

// ---------- the box's setup screens ----------
// Scan, DX, Roku, Inputs and Config are screens of one menu on the box (settop.css's
// .setup-room): the TV shows the menu's tabs, the Explorer above it its front panel.

const SETUP_SCREENS = [
  ["rescan.html", "AUTO PROGRAM"], ["dx.html", "SIGNAL METER"], ["roku.html", "ROKU"],
  ["inputs.html", "INPUTS"], ["config.html", "SETTINGS"],
];

function setupScreen() {
  const here = location.pathname.split("/").pop() || "index.html";
  return SETUP_SCREENS.findIndex(([href]) => href === here);
}

// The menu's tabs in ``nav``, this page's selected.
function setupMenu(nav) {
  const here = setupScreen();
  nav.replaceChildren(...SETUP_SCREENS.map(([href, label], index) => {
    const tab = document.createElement("a");
    tab.href = href;
    tab.textContent = `${index + 1} ${label}`;
    if (index === here) tab.setAttribute("aria-current", "page");
    return tab;
  }));
}

// The Explorer in ``section`` for a screen with no keys of its own: ``model`` under the
// badge, ``lamps`` ([key, label, amber?]) in its window. Its pad moves between the menu's
// screens (◀ ▶), scrolls this one (▲ ▼) and reads it again (SELECT). Returns its panel
// (explorerPanel): the page lights the lamps and writes the line.
function setupBox(section, { model, lamps = [] }) {
  const lamp = ([key, label, amber]) =>
    `<span class="lamp${amber ? " amber" : ""}" data-lamp="${key}">${label}</span>`;
  section.innerHTML = `
    <div class="sa-badge">
      <span class="sa-maker">SCIENTIFIC ATLANTA</span>
      <span class="sa-explorer">Explorer</span>
      <span class="sa-model">${model}</span>
    </div>
    <div class="settop-window" role="status" aria-live="polite" aria-label="Box display">
      <div class="lamps" aria-hidden="true">${lamps.map(lamp).join("")}</div>
      <svg class="seg settop-clock" aria-hidden="true"></svg>
      <div class="settop-text"><span>${model}</span></div>
    </div>
    <div class="sa-controls">
      <div class="sa-pad" role="group" aria-label="Menu">
        <button class="sa-arrow up" data-pad="up" title="Scroll up" aria-label="Scroll up">▲</button>
        <button class="sa-arrow left" data-pad="left" title="Previous screen" aria-label="Previous screen">◀</button>
        <button class="sa-select" data-pad="select" title="Read this screen again">SELECT</button>
        <button class="sa-arrow right" data-pad="right" title="Next screen" aria-label="Next screen">▶</button>
        <button class="sa-arrow down" data-pad="down" title="Scroll down" aria-label="Scroll down">▼</button>
      </div>
      <div class="sa-keys">
        <a class="nub" href="guide.html" title="The program guide">GUIDE</a>
        <a class="nub" href="vcr.html" title="Recordings and timers">VCR</a>
        <a class="nub" href="about.html" title="About TV Thing">INFO</a>
        <a class="nub" href="./" title="Back to watching">EXIT</a>
      </div>
    </div>
    <span class="settop-power" aria-hidden="true"><span class="led on green"></span></span>`;
  const here = setupScreen();
  const go = (by) => {
    const next = SETUP_SCREENS[(here + by + SETUP_SCREENS.length) % SETUP_SCREENS.length];
    location.href = next[0];
  };
  const scroll = (by) => {
    const step = by * Math.round(window.innerHeight * 0.6);
    (document.scrollingElement || document.documentElement).scrollBy({ top: step, behavior: "smooth" });
  };
  const actions = { left: () => go(-1), right: () => go(1), up: () => scroll(-1), down: () => scroll(1),
                    select: () => location.reload() };
  section.querySelectorAll("[data-pad]").forEach(button =>
    button.addEventListener("click", () => actions[button.dataset.pad]()));
  return explorerPanel(section.querySelector(".settop-window"));
}
