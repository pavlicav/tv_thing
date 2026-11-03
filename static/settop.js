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
