// Named extension points. Lanes register into them instead of editing each other's modules.
//
// Names defined by F: topbar.account, home.top, playlist.actions, playlist.menu, playlist.related,
// playlist.footer, row.menu, crate.actions, crate.row.menu, auth.required, share.open, import.open,
// player.like, player.more (phone player ⋯ menu items), rail.context.
//
//   register(name, fn, { order })  fn(ctx) returns a TemplateResult (render slots), or menu items
//                                   (playlist.menu, row.menu, crate.row.menu), or a value (call slots)
//   render(name, ctx)               every registration's result, by ascending order; [] when none
//   call(name, ...args)             the first registration's return value; undefined when none,
//                                   so the caller can use its own fallback
//   has(name)

const registry = new Map();
let seq = 0;

export function register(name, fn, { order = 0 } = {}) {
  if (typeof fn !== 'function') throw new TypeError(`slots.register(${name}): fn must be a function`);
  const list = registry.get(name) || [];
  const entry = { fn, order, seq: seq++ };
  list.push(entry);
  list.sort((a, b) => a.order - b.order || a.seq - b.seq);
  registry.set(name, list);
  return () => {
    const cur = registry.get(name) || [];
    registry.set(name, cur.filter((e) => e !== entry));
  };
}

export function has(name) {
  return (registry.get(name) || []).length > 0;
}

export function render(name, ctx) {
  const out = [];
  for (const { fn } of registry.get(name) || []) {
    try {
      const r = fn(ctx);
      if (r == null || r === false) continue;
      if (Array.isArray(r)) out.push(...r);
      else out.push(r);
    } catch (err) {
      console.error(`slot ${name}`, err);
    }
  }
  return out;
}

export function call(name, ...args) {
  const list = registry.get(name) || [];
  if (!list.length) return undefined;
  return list[0].fn(...args);
}
