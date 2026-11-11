// The only importer of vendor/lit-html (3.2.1). Templates auto-escape every binding.
// Never add unsafeHTML / unsafeSVG / unsafeStatic / templateContent.
export { html, svg, render, nothing, noChange } from '../vendor/lit-html/lit-html.js';
export { repeat } from '../vendor/lit-html/directives/repeat.js';
export { classMap } from '../vendor/lit-html/directives/class-map.js';
export { live } from '../vendor/lit-html/directives/live.js';
export { ref, createRef } from '../vendor/lit-html/directives/ref.js';
